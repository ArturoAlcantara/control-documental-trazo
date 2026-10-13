import base64
import json
import os
import time
import random
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
    if es_administrador():
        return True
    asignado = responsable_de_sesion()
    return bool(asignado) and normalizar_texto(asignado) == normalizar_texto(responsable)


def exigir_permiso_edicion(responsable):
    if not puede_editar_responsable(responsable):
        raise PermissionError("Solo puedes modificar oficios asignados a tu cuenta.")


def exigir_sesion():
    if not st.session_state.get("autenticado", False):
        raise PermissionError("Debes iniciar sesión para realizar esta operación.")


def navegar_a(destino):
    """Cambia de módulo y actualiza la interfaz sin perder la sesión."""
    destinos = {"home", "oficios", "diseno", "pendientes"}
    if destino not in destinos:
        raise ValueError("Módulo de navegación no reconocido.")
    st.session_state.menu_activo = destino
    st.rerun()


# =========================================================
# AUTENTICACIÓN
# =========================================================

def mostrar_login():
    st.markdown(
        """
        <h4 style="text-align:center; color:#B08A4A; letter-spacing:3px;">
            MESA DE DISEÑO GEOMÉTRICO
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
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")
            except KeyError:
                st.error("Usuario o contraseña incorrectos.")


def mostrar_sesion():
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
            st.session_state.autenticado = False
            st.session_state.usuario_actual = None
            st.session_state.usuario_id = None
            st.session_state.rol = "auxiliar"
            st.session_state.cargo_institucional = ""
            st.session_state.responsable_oficios = ""
            st.session_state.menu_activo = "home"
            st.rerun()


# =========================================================
# BLOQUEO DE ACCESO
# =========================================================

if not st.session_state.autenticado:
    mostrar_login()
    st.stop()


BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"


# =========================================================
# CONEXIÓN A GOOGLE SHEETS (HÍBRIDA: LOCAL + SECRETS)
# =========================================================

@st.cache_resource(show_spinner=False)
def conectar_google_sheets():
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
        for intento in range(4):
            try:
                return solicitud_original(*args, **kwargs)
            except gspread.exceptions.APIError as error:
                codigo = getattr(getattr(error, "response", None), "status_code", None)
                # Reintentar errores de cuota; las escrituras con errores 5xx
                # no se repiten automáticamente para evitar duplicados.
                if codigo != 429 or intento == 3:
                    raise
                time.sleep(min(2 ** intento + random.uniform(0, 0.6), 9))

    transporte.request = solicitud_con_reintentos

    try:
        spreadsheet_id = "1jweDu5kKkF2onZArr8-QV_CgwO1tPzfMBtdkEeHRQgY"
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


def _credenciales_drive():
    """Drive personal usa OAuth del propietario, no la cuenta de servicio de Sheets.

    Ejecuta configurar_drive_oauth.py una sola vez para obtener el token.
    En despliegues remotos se puede configurar [drive_oauth] en secrets.toml.
    """
    alcance = ["https://www.googleapis.com/auth/drive"]
    token_local = BASE_DIR / "drive_oauth_token.json"
    if token_local.is_file():
        datos = json.loads(token_local.read_text(encoding="utf-8"))
    else:
        try:
            datos = dict(st.secrets["drive_oauth"])
        except (KeyError, FileNotFoundError) as error:
            raise RuntimeError(
                "Falta configurar [drive_oauth] en los Secrets de Streamlit Cloud. "
                "Agrega los campos token, refresh_token, token_uri, client_id y client_secret "
                "de tu autorización OAuth privada (sin subir el JSON a GitHub). "
                "En localhost puedes ejecutar configurar_drive_oauth.py."
            ) from error
    credenciales = OAuthCredentials.from_authorized_user_info(datos, scopes=alcance)
    if not credenciales.valid:
        if credenciales.refresh_token:
            try:
                credenciales.refresh(GoogleAuthRequest())
            except Exception as error:
                raise RuntimeError(
                    "Google Drive rechazó la renovación OAuth. Reautoriza la cuenta "
                    "y actualiza [drive_oauth] en Streamlit Secrets (refresh_token vigente)."
                ) from error
            if token_local.is_file():
                token_local.write_text(credenciales.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(
                "La autorización de Drive no tiene refresh_token válido. "
                "Vuelve a autorizar la cuenta y actualiza [drive_oauth] en Streamlit Secrets."
            )
    return credenciales


def _carpeta_raiz_borradores():
    """Obtiene el ID de carpeta desde Streamlit o secrets.toml local.

    La lectura directa del archivo permite detectar cambios locales aun si
    Streamlit todavía conserva una versión anterior de sus secretos.
    """
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


@st.cache_resource(show_spinner=False)
def _hoja_borradores():
    hoja_oficios = conectar_google_sheets()
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


@st.cache_data(ttl=25, show_spinner=False)
def _leer_registros_borradores():
    return _hoja_borradores().get_all_records()


@st.cache_data(ttl=25, show_spinner=False)
def _leer_filas_borradores():
    return _hoja_borradores().get_all_values()


@st.cache_data(ttl=25, show_spinner=False)
def _leer_registros_eventos():
    return _hoja_eventos_borradores().get_all_records()


@st.cache_data(ttl=25, show_spinner=False)
def _leer_registros_oficios():
    return conectar_google_sheets().get_all_records()


def _invalidar_cache_documental(borradores=False, eventos=False, oficios=False):
    if borradores:
        _leer_registros_borradores.clear()
        _leer_filas_borradores.clear()
    if eventos:
        _leer_registros_eventos.clear()
    if oficios:
        _leer_registros_oficios.clear()
        obtener_oficios.clear()


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


@st.cache_resource(show_spinner=False)
def _hoja_eventos_borradores():
    """Bitácora independiente; nunca altera las columnas de BORRADORES."""
    libro = conectar_google_sheets().spreadsheet
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
        if not puede_gestionar_borradores():
            raise PermissionError("Tras la primera carga, las nuevas versiones corresponden a John y Alejandra.")
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
    return subido


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
         and etapa_nueva == ETAPA_TTE and admin) or
        (actual == ETAPA_TTE and etapa_nueva in (ETAPA_CORRECCIONES, ETAPA_FIRMA) and tte) or
        (actual == ETAPA_FIRMA and etapa_nueva in (ETAPA_RETORNO_FIRMA, ETAPA_ARCHIVADO) and admin)
    )
    if not permitido:
        raise PermissionError("La transición no está permitida para tu usuario o la etapa actual.")
    if etapa_nueva in (ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA) and not detalle.strip():
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
        # Los revisores gestionan el documento, no cambian el responsable.
        if not puede_gestionar_borradores():
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


def _mensaje_error_sheets(error):
    mensaje = str(error)
    if "429" in mensaje or "RATE_LIMIT_EXCEEDED" in mensaje or "RESOURCE_EXHAUSTED" in mensaje:
        return ("Google Sheets alcanzó temporalmente su límite de consultas. "
                "Espera aproximadamente un minuto y vuelve a abrir el expediente "
                "para comprobar la etapa antes de intentar enviarlo de nuevo.")
    return mensaje


def _mostrar_observaciones_revisor(folio, numero, etapa):
    """Muestra las indicaciones conservadas en EVENTOS_BORRADORES."""
    import html
    if not puede_gestionar_borradores() or etapa not in (ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA):
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
        st.caption(f"Versión V{int(ultimo['Version']):02d} · {len(versiones)} archivo(s) conservado(s)")
    _mostrar_observaciones_revisor(folio, numero, etapa)
    if ultimo:
        st.markdown(f"**📄 {html.escape(str(ultimo.get('Nombre', 'Documento Word')))}**")
        st.caption(f"Registrado por: {ultimo.get('Usuario', '')} · {ultimo.get('Fecha registro', '')}")
        ver, descargar = st.columns(2)
        with ver:
            st.link_button("Consultar en Google Drive ↗", str(ultimo.get("URL", "")), use_container_width=True)
        with descargar:
            if st.button("Preparar descarga para Word ↓", key=f"doc_preparar_{folio}_{numero}", use_container_width=True):
                try:
                    with st.spinner("Descargando desde Google Drive..."):
                        st.session_state[f"doc_bytes_{folio}_{numero}"] = _descargar_docx_drive(ultimo["Drive ID"])
                        st.session_state[f"doc_id_{folio}_{numero}"] = ultimo["Drive ID"]
                except Exception as error:
                    st.error(str(error))
            if (st.session_state.get(f"doc_id_{folio}_{numero}") == ultimo["Drive ID"]
                    and f"doc_bytes_{folio}_{numero}" in st.session_state):
                st.download_button("Descargar .docx", st.session_state[f"doc_bytes_{folio}_{numero}"],
                                   file_name=str(ultimo["Nombre"]),
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                   key=f"doc_descargar_{folio}_{numero}", use_container_width=True)
        with st.expander(f"Historial completo de versiones · {len(versiones)}/15", expanded=False):
            try:
                movimientos = _eventos_expediente(folio, numero)
            except Exception as error:
                movimientos = []
                st.warning(f"No se pudo consultar la bitácora: {_mensaje_error_sheets(error)}")
            for version in reversed(versiones):
                n = int(version.get("Version") or 0)
                st.markdown(f"**V{n:02d} · {html.escape(str(version.get('Nombre', 'Word')))}**")
                st.caption(f"Cargada: {version.get('Fecha registro', '—')} · "
                           f"Por: {version.get('Usuario', '—')} · "
                           f"Etapa registrada: {version.get('Etapa', '—')}")
                enlace = str(version.get("URL", ""))
                if enlace.startswith("https://"):
                    st.link_button(f"Consultar Word V{n:02d} en Drive ↗", enlace,
                                   key=f"hist_drive_{folio}_{numero}_{n}")
                relacionados = [e for e in movimientos if int(e.get("Version") or 0) == n]
                for e in relacionados:
                    st.caption(f"{e.get('Fecha', '—')} · {e.get('Usuario', '—')} · "
                               f"{e.get('Accion', '—')}: {e.get('Detalle', '')}")
                st.divider()
    else:
        st.info("Aún no hay un Word registrado. Puedes comenzar aquí, incluso si el archivo llegó por USB o correo.")

    try:
        eventos = _eventos_expediente(folio, numero) if ultimo else []
    except Exception as error:
        eventos = []
        st.caption(f"Bitácora temporalmente no disponible: {error}")
    if eventos:
        with st.expander(f"Bitácora de movimientos · {len(eventos)} eventos"):
            for evento in reversed(eventos):
                st.markdown(f"**{html.escape(str(evento['Accion']))}** · "
                            f"V{int(evento['Version']):02d} · {html.escape(str(evento['Usuario']))}")
                st.caption(f"{evento['Fecha']} · {evento.get('Detalle', '')}")

    admin = puede_gestionar_borradores()
    tte = puede_realizar_revision_final()
    auxiliar = puede_editar_responsable(responsable) and not (admin or tte)
    puede_subir = ((not ultimo and (admin or auxiliar)) or
                   (ultimo and admin and etapa in (ETAPA_BORRADOR, ETAPA_ENVIADO,
                                                    ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA)))
    if puede_subir:
        with st.expander("＋ Cargar primera versión" if not ultimo else "＋ Cargar nueva versión corregida", expanded=not ultimo):
            with st.form(key=f"doc_carga_{folio}_{numero}"):
                archivo = st.file_uploader("Documento Word editable (.docx)", type=["docx"],
                                           key=f"doc_archivo_{folio}_{numero}")
                procedencia = st.selectbox("¿Cómo se recibió o elaboró el documento?",
                                           ["Elaboración propia", "Memoria USB", "Correo electrónico",
                                            "Entregado por auxiliar", "Otro medio"],
                                           key=f"doc_procedencia_{folio}_{numero}")
                personas = ["No especificado", "Magali Rivera", "Ricardo Godínez", "Cecilia Santos",
                            "Jesús Rodriguez", "Alejandra Cardoso", "John Bautista", "Otro"]
                persona_elegida = st.selectbox("¿Quién lo elaboró o entregó?", personas,
                                              key=f"doc_origen_lista_{folio}_{numero}")
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
        if admin:
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
    elif etapa in (ETAPA_ENVIADO, ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA) and admin:
        st.info("John y Alejandra pueden cargar una versión corregida o enviar la actual a la Tte.")
        if st.button("Enviar versión actual a la Tte. Alondra →", key=f"doc_reenviar_{folio}_{numero}",
                     type="primary", use_container_width=True):
            try:
                _cambiar_etapa_borrador(folio, numero, ETAPA_TTE)
                st.rerun()
            except Exception as error:
                st.error(_mensaje_error_sheets(error))
    elif etapa == ETAPA_TTE and tte:
        with st.form(key=f"doc_revision_tte_{folio}_{numero}"):
            decision = st.radio("Resultado de la revisión", ["Autorizar para firma", "Solicitar correcciones"],
                                horizontal=True)
            comentario = st.text_area("Observaciones de revisión", placeholder="Indica los ajustes requeridos")
            enviar = st.form_submit_button("Registrar decisión", type="primary", use_container_width=True)
        if enviar:
            try:
                nueva = ETAPA_FIRMA if decision == "Autorizar para firma" else ETAPA_CORRECCIONES
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


@st.cache_data(ttl=60, show_spinner=False)
def obtener_oficios():
    try:
        registros = _leer_registros_oficios()
        return pd.DataFrame(registros)
    except Exception as error:
        st.warning(f"No se pudo consultar Google Sheets: {error}")
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
    if not es_administrador():
        raise PermissionError("Solo las cuentas con rol administrador o propietario pueden eliminar oficios.")
    hojas = {
        "OFICIOS": conectar_google_sheets(),
        "BORRADORES": _hoja_borradores(),
        "EVENTOS_BORRADORES": _hoja_eventos_borradores(),
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
        "Eliminado de Sheets; archivos de Google Drive conservados"], value_input_option="RAW")
    # Eliminar de abajo arriba; si hay fallo, la auditoría permite reconstruir el incidente.
    for nombre in ("EVENTOS_BORRADORES", "BORRADORES", "OFICIOS"):
        for indice in sorted(filas_borrar[nombre], reverse=True):
            hojas[nombre].delete_rows(indice)
    _invalidar_cache_documental(borradores=True, eventos=True, oficios=True)
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


def _panel_eliminar_oficio(folio, numero, *, origen):
    """Confirmación doble reutilizable en tabla y ficha de búsqueda."""
    if not es_administrador():
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
    if columna == "Responsable" and not es_administrador():
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
    ruta_logo = Path(__file__).parent / "assets" / "logo_diseno_geometrico.png"
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
        canvas_obj.drawString(45, alto - 38, "CONTROL DOCUMENTAL - MESA DE DISEÑO GEOMÉTRICO")
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
            celda(fila.get("Responsable", "")),
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
# FORMULARIO DE REGISTRO
# =========================================================

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

    col1, col2, col3 = st.columns([1, 1.25, 1])

    with col1:
        folio = st.text_input(
            "Folio",
            key=f"folio_{version}"
        )

    with col2:
        numero = st.text_input(
            "No. de oficio",
            key=f"numero_{version}"
        )

    with col3:
        responsable = st.selectbox(
            "Responsable",
            [
                "Alejandra",
                "Alex",
                "Jesús",
                "Cecilia",
                "Tte. Barajas",
                "Ricardo",
                "Magali",
                "John",
                "Karina"
            ],
            index=None,
            placeholder="Seleccionar responsable",
            key=f"responsable_{version}"
        )

    col4, col5 = st.columns([1, 1.6])

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
        estatus = st.selectbox(
            "Estatus", ESTATUS_OFICIOS, key=f"estatus_{version}"
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

    # Oficio firmado
    st.markdown(
        """
        <div style="
            color:#E8F0F5;
            font-size:11px;
            font-weight:700;
            margin-top:10px;
            margin-bottom:4px;
        ">
            ▣ Oficio firmado
        </div>
        """,
        unsafe_allow_html=True
    )

    col_firmado_nombre, col_firmado_link = st.columns([1, 1.6])

    with col_firmado_nombre:
        nombre_firmado = st.text_input(
            "Nombre del documento",
            placeholder="Ej. LFSLPSTL-IB-26-2119",
            key=f"nuevo_nombre_firmado_{version}",
            disabled=not es_firmado
        )

    with col_firmado_link:
        liga_firmado = st.text_input(
            "Enlace de Drive",
            placeholder="Pega aquí el enlace de Drive...",
            key=f"nuevo_liga_firmado_{version}",
            disabled=not es_firmado
        )

    # Construir el JSON o string requerido para el guardado de 'firmado'
    if es_firmado and (nombre_firmado.strip() or liga_firmado.strip()):
        firmado = json.dumps([{
            "nombre": nombre_firmado.strip(),
            "url": liga_firmado.strip()
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
        antecedentes = documentos_formulario(f"antecedentes_{version}")

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

        # Validar campos obligatorios
        if not folio.strip() or not numero.strip():
            st.error(
                "Debes capturar el folio y número de oficio."
            )
            return

        if not responsable:
            st.error("Debes seleccionar un responsable.")
            return

        if es_firmado and fecha_entrega is None:
            st.error("Para marcar un oficio como Firmado debes indicar su fecha de entrega.")
            return

        try:
            with st.spinner("Guardando oficio en Google Sheets..."):

                # Consultar registros existentes
                existentes = obtener_oficios()

                # Verificar folio duplicado
                if (
                    not existentes.empty
                    and "Folio" in existentes.columns
                    and folio.strip()
                    in existentes["Folio"].astype(str).str.strip().values
                ):
                    st.error(
                        "Ya existe un oficio registrado con ese folio."
                    )
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

                # Registrar en Google Sheets
                guardar_oficio(datos)

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


def mostrar_documentos(oficio):
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

        st.markdown(f'<div class="tte-document-line"><span>📄</span><div>'
                    f'<strong>{html.escape(nombre)}</strong>'
                    '<small>Archivo Word · Versión vigente para revisión</small>'
                    '</div></div>', unsafe_allow_html=True)

        ver, descargar = st.columns(2, gap="small")
        with ver:
            if url.startswith("https://"):
                st.link_button("↗ Consultar en Google Drive", url, use_container_width=True)
            else:
                st.caption("Enlace de Drive no disponible")
        with descargar:
            if st.button("↓ Preparar descarga Word", key=f"tte_prepare_{clave}",
                         use_container_width=True):
                try:
                    with st.spinner("Preparando documento..."):
                        st.session_state[f"tte_bytes_{clave}"] = _descargar_docx_drive(drive_id)
                        st.session_state[f"tte_drive_id_{clave}"] = drive_id
                except Exception as error:
                    st.error(f"No se pudo preparar el documento: {error}")
            if st.session_state.get(f"tte_drive_id_{clave}") == drive_id and f"tte_bytes_{clave}" in st.session_state:
                st.download_button("Descargar archivo .docx", st.session_state[f"tte_bytes_{clave}"],
                                   file_name=nombre,
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                   key=f"tte_download_{clave}", use_container_width=True)

        with st.expander("Ver versiones anteriores y bitácora"):
            try:
                versiones = _borradores_expediente(folio, numero)
                for v in reversed(versiones):
                    link = str(v.get("URL", ""))
                    etiqueta = f"V{int(v.get('Version') or 0):02d} · {v.get('Nombre', '')}"
                    if link.startswith("https://"):
                        st.markdown(f"[{html.escape(etiqueta)}]({link})")
                    else:
                        st.write(etiqueta)
                eventos = _eventos_expediente(folio, numero)
                for evento in reversed(eventos):
                    st.caption(f"V{int(evento.get('Version') or 0):02d} · {evento.get('Fecha', '')} · "
                               f"{evento.get('Accion', '')} · {evento.get('Usuario', '')} · "
                               f"{evento.get('Detalle', '')}")
            except Exception as error:
                st.caption(f"Historial temporalmente no disponible: {_mensaje_error_sheets(error)}")

        st.markdown('<div class="tte-decision-title">Decisión sobre este documento</div>'
                    '<div class="tte-decision-sub">Seleccione una opción y confirme para registrar el resultado.</div>',
                    unsafe_allow_html=True)
        decision = st.radio("Resultado de la revisión",
                            ["Autorizar para firma", "Solicitar correcciones"],
                            horizontal=True, key=f"tte_decision_{clave}",
                            label_visibility="collapsed")
        with st.form(key=f"tte_form_{clave}"):
            if decision == "Solicitar correcciones":
                comentario = st.text_area("Indique los cambios que deben realizarse *",
                                          placeholder="Describa los ajustes concretos para John y Alejandra...",
                                          key=f"tte_observaciones_{clave}")
            else:
                comentario = st.text_area("Observaciones adicionales (opcional)",
                                          placeholder="Puede agregar alguna indicación para el trámite de firma...",
                                          key=f"tte_observaciones_{clave}")
            confirmado = st.checkbox(
                "Confirmo que revisé el documento y deseo registrar esta decisión.",
                key=f"tte_confirmar_{clave}")
            enviar = st.form_submit_button(
                "✓ Autorizar y enviar a firma" if decision == "Autorizar para firma"
                else "↩ Solicitar correcciones",
                type="primary", use_container_width=True)
        if enviar:
            if not confirmado:
                st.warning("Marque la confirmación antes de registrar su decisión.")
            elif decision == "Solicitar correcciones" and not comentario.strip():
                st.warning("Indique las correcciones requeridas antes de continuar.")
            else:
                try:
                    nueva_etapa = ETAPA_FIRMA if decision == "Autorizar para firma" else ETAPA_CORRECCIONES
                    with st.spinner("Registrando decisión..."):
                        _actualizar_oficio_flujo(folio, numero, {
                            "Estatus": "En firma" if nueva_etapa == ETAPA_FIRMA else "En correcciones"
                        }, autorizacion_tte=True)
                        _cambiar_etapa_borrador(folio, numero, nueva_etapa, comentario.strip())
                    st.success("Decisión registrada correctamente.")
                    st.rerun()
                except Exception as error:
                    st.error(_mensaje_error_sheets(error))


def _bandeja_borradores_compartida():
    """Bandeja operativa con acciones para cada revisor."""
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
        st.markdown('<div class="tte-queue-header"><div class="tte-queue-eyebrow">MESA DE DISEÑO GEOMÉTRICO · AUTORIZACIONES</div>'
                    '<div class="tte-queue-heading">Centro de autorizaciones</div>'
                    '<div class="tte-queue-description">Revise los documentos recibidos y registre su decisión en cada expediente.</div>'
                    '</div>', unsafe_allow_html=True)
        # Resumen superior: tres indicadores centrados en tarjetas grises.
        # No modificar el encabezado de cada expediente.
        st.markdown("""
        <style>
        .tte-summary-card {
            background:linear-gradient(145deg,#424546,#555859);
            border:1px solid #747674;
            border-left:4px solid #c79d59;
            border-radius:12px;
            min-height:112px;
            padding:18px 12px 15px;
            box-shadow:0 5px 14px rgba(25,26,26,.13);
            display:flex;flex-direction:column;align-items:center;
            justify-content:center;text-align:center;gap:7px;
        }
        .tte-summary-label {
            color:#e3e3e1 !important;font-size:13px !important;
            font-weight:650 !important;line-height:1.3 !important;
        }
        .tte-summary-value {
            color:#fff !important;font-size:27px !important;
            line-height:1.1 !important;font-weight:800 !important;
            overflow-wrap:anywhere;
        }
        .tte-summary-card--final .tte-summary-value {font-size:22px !important;}
        @media(max-width:640px){
            .tte-summary-card {min-height:94px;padding:12px 6px;}
            .tte-summary-label {font-size:11px !important;}
            .tte-summary-value {font-size:21px !important;}
        }
        </style>
        """, unsafe_allow_html=True)
        m1, m2, m3 = st.columns(3, gap="small")
        with m1:
            st.markdown(f'<div class="tte-summary-card"><div class="tte-summary-label">Por revisar</div>'
                        f'<div class="tte-summary-value">{len(recibidos):02d}</div></div>',
                        unsafe_allow_html=True)
        with m2:
            st.markdown(f'<div class="tte-summary-card"><div class="tte-summary-label">Documentos recibidos</div>'
                        f'<div class="tte-summary-value">{len(ultimos):02d}</div></div>',
                        unsafe_allow_html=True)
        with m3:
            st.markdown('<div class="tte-summary-card tte-summary-card--final">'
                        '<div class="tte-summary-label">Tipo de revisión</div>'
                        '<div class="tte-summary-value">Final</div></div>',
                        unsafe_allow_html=True)
        if recibidos:
            busqueda = st.text_input("Buscar documento", placeholder="Buscar por folio o número de oficio...",
                                     key="tte_busqueda_revision")
            if busqueda.strip():
                filtro = normalizar_texto(busqueda)
                recibidos = [r for r in recibidos if filtro in normalizar_texto(
                    f"{r.get('Folio', '')} {r.get('No. de oficio', '')} {r.get('Nombre', '')}")]
        if not recibidos:
            st.success("No hay documentos pendientes de su revisión.")
            return
        st.caption(f"{len(recibidos)} documento(s) en la bandeja · Seleccione la decisión dentro de cada expediente.")
        for r in recibidos:
            _panel_revision_tte(r)
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
    st.markdown("#### En proceso de firma")
    st.caption("La firma ocurre fuera de la plataforma. Aquí solo consultamos los oficios enviados.")
    firmando = registros.loc[registros["Estatus"].fillna("").astype(str).str.strip() == "En firma"]
    if firmando.empty:
        st.info("No hay oficios registrados actualmente en firma.")
        return
    for _, r in firmando.iterrows():
        with st.container(border=True):
            st.markdown(f"**{r.get('No. de oficio', '')}** · Folio {r.get('Folio', '')}")
            st.caption(f"Responsable: {r.get('Responsable', '')} · En firma externa")
            st.write(str(r.get("Asunto", "")))


def mostrar_mis_pendientes():
    """Acceso rápido a los oficios activos, respetando los permisos existentes."""
    import html

    mostrar_sesion()
    st.markdown("""
    <div class="pend-hero">
      <div class="pend-hero-kicker">MESA DE DISEÑO GEOMÉTRICO · CONTROL DOCUMENTAL</div>
      <div class="pend-hero-title">Mis pendientes</div>
      <div class="pend-hero-desc">Borradores, revisiones y documentos en proceso de firma</div>
      <div class="pend-hero-line"></div>
      <div class="pend-hero-track">TRAZO · ALINEAMIENTO · RASANTE</div>
    </div>
    """, unsafe_allow_html=True)

    registros = obtener_oficios()
    if registros.empty:
        st.info("No hay registros disponibles o no se pudo consultar la hoja OFICIOS.")
        if st.button("← Volver a Oficios en trámite", key="pendientes_volver_vacio"):
            navegar_a("oficios")
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

    etiquetas = ["Mis oficios"]
    if puede_gestionar_borradores() or puede_realizar_revision_final():
        etiquetas.append("Por revisar")
    etiquetas.append("En proceso de firma")
    # Navegación por bandejas: controles reales, sin pestañas estrechas.
    if "pend_bandeja" not in st.session_state or st.session_state.pend_bandeja not in etiquetas:
        st.session_state.pend_bandeja = "Mis oficios"
    bandeja_actual = st.session_state.pend_bandeja
    st.markdown('<div class="pend-nav-label">BANDEJAS DE TRABAJO</div>', unsafe_allow_html=True)
    opciones_nav = [
        ("Mis oficios", "▣", "Expedientes a mi cargo"),
        ("Por revisar", "◉", "Documentos por validar"),
        ("En proceso de firma", "▤", "Seguimiento de firmas"),
        ("Nuevo oficio", "+", "Registrar expediente"),
    ]
    cols_nav = st.columns(4, gap="small")
    for i, (nombre, icono, descripcion) in enumerate(opciones_nav):
        with cols_nav[i]:
            if nombre not in etiquetas and nombre != "Nuevo oficio":
                st.markdown('<div class="pend-nav-disabled">Acceso no disponible</div>', unsafe_allow_html=True)
                continue
            activo = nombre == bandeja_actual
            with st.container(key=f"pend_nav_{i}_{'activo' if activo else 'normal'}"):
                st.markdown(f'<div class="pend-nav-caption">{descripcion}</div>', unsafe_allow_html=True)
                if st.button(f"{icono}  {nombre}", key=f"pend_nav_btn_{i}", use_container_width=True):
                    if nombre == "Nuevo oficio":
                        navegar_a("oficios")
                    else:
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
                        if not autorizado:
                            st.info("Tienes acceso de consulta, pero no permiso para editar este oficio.")
                        else:
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
                        # Mostrar documentos existentes después del formulario.
                        mostrar_documentos(oficio)

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
    border: none;
    border-top: 2px solid #B08A4A;
    border-radius: 10px 10px 0 0;
    padding: 16px 20px 12px 20px;
    margin-top: 20px;
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
# MENÚ LATERAL COMPARTIDO: OFICIOS / MIS PENDIENTES
# =========================================================
# Mantener la misma estructura de widgets y las mismas claves entre
# pantallas evita desmontajes innecesarios durante la navegación.
if st.session_state.menu_activo in ("oficios", "pendientes"):
    # Imagen de la barra lateral: independiente del fondo del área principal.
    # Se incluye dentro del CSS para que funcione en Streamlit Cloud y localhost.
    ruta_fondo_sidebar = next((ruta for ruta in (
        ASSETS_DIR / "fondo_ferroviario.png",
        ASSETS_DIR / "fondo_pendientes.png",
        ASSETS_DIR / "fondo_oficios.png",
    ) if ruta.is_file()), None)
    fondo_sidebar_css = ""
    if ruta_fondo_sidebar is not None:
        extension = "jpeg" if ruta_fondo_sidebar.suffix.lower() in (".jpg", ".jpeg") else "png"
        imagen_sidebar = base64.b64encode(ruta_fondo_sidebar.read_bytes()).decode("ascii")
        fondo_sidebar_css = (
            'background-image: linear-gradient(rgba(250,249,246,.87), '
            'rgba(250,249,246,.91)), '
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
    [data-testid="stSidebar"] .stButton button {
        background: #283e50 !important;
        color: #fff !important;
        border: 0 !important;
        border-radius: 7px !important;
        min-height: 43px !important;
        font-weight: 600 !important;
    }
    [data-testid="stSidebar"] .stButton button:hover {
        background: #40576b !important;
        color: #fff !important;
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
    """.replace("__FONDO_SIDEBAR__", fondo_sidebar_css), unsafe_allow_html=True)
    with st.sidebar:
        ruta_logo = BASE_DIR / "assets" / "logo_diseno_geometrico.png"
        if ruta_logo.is_file():
            # Widget nativo en vez de inyectar <img> con una cadena base64.
            _, columna_logo, _ = st.columns([1, 2, 1])
            with columna_logo:
                st.image(str(ruta_logo), use_container_width=True)
        st.markdown(
            '<div class="cd-sidebar-title">CONTROL DOCUMENTAL</div>'
            '<div class="cd-sidebar-subtitle">Mesa de Diseño Geométrico</div>',
            unsafe_allow_html=True,
        )
        if st.button("⌂ Menú principal", key="nav_compartida_inicio", use_container_width=True):
            navegar_a("home")
        if st.button("▤ Mis pendientes", key="nav_compartida_pendientes", use_container_width=True):
            if st.session_state.menu_activo != "pendientes":
                navegar_a("pendientes")
        st.divider()

# Fondo de trabajo: conserva el plano ferroviario visible detrás de las tarjetas.
# Los módulos mantienen sus propias imágenes y la navegación principal no cambia.

# =========================================================
# HOME
# =========================================================

if st.session_state.menu_activo == "home":
    aplicar_fondo()
    mostrar_sesion()

    def imagen_menu_base64(nombre):
        ruta = Path(__file__).parent / "assets" / nombre
        if ruta.is_file():
            return "data:image/png;base64," + base64.b64encode(ruta.read_bytes()).decode("ascii")
        return ""

    imagen_oficios = imagen_menu_base64("menu_oficios.png")
    imagen_ingenieria = imagen_menu_base64("menu_ingenieria.png")
    if not imagen_oficios or not imagen_ingenieria:
        faltantes = [
            nombre for nombre, valor in (
                ("menu_oficios.png", imagen_oficios),
                ("menu_ingenieria.png", imagen_ingenieria),
            ) if not valor
        ]
        st.warning("Faltan imágenes en la carpeta assets: " + ", ".join(faltantes))

    st.html(
        """
        <style>
        .home-hero {
            text-align: center;
            padding: 12px 0 24px;
            color: #10283b;
        }

        .home-kicker {
            font-size: 10px;
            font-weight: 750;
            letter-spacing: 2.5px;
            color: #95743c;
        }

        .home-title {
            font-size: clamp(34px, 4vw, 48px);
            font-weight: 850;
            letter-spacing: 2px;
            line-height: 1.05;
            margin: 10px 0 5px;
        }

        .home-subtitle {
            font-size: clamp(18px, 2.5vw, 26px);
            font-weight: 350;
            letter-spacing: 7px;
        }

        .home-line {
            width: 170px;
            height: 2px;
            background: #b39258;
            margin: 18px auto 0;
        }

        .home-module-label {
            font-size: 11px;
            letter-spacing: 2px;
            color: #536574;
            font-weight: 750;
            margin-bottom: 12px;
        }

        .stMainBlockContainer:has(.home-hero) {
            max-width: 1120px !important;
            padding-top: 1rem !important;
        }

        /* TARJETAS DEL MENÚ PRINCIPAL */

        .st-key-home_oficios button,
        .st-key-home_diseno button {
            position: relative !important;
            display: flex !important;
            flex-direction: column !important;
            justify-content: center !important;
            align-items: flex-start !important;

            width: 100% !important;
            height: 205px !important;
            min-height: 205px !important;

            padding: 22px 20px 22px 51% !important;
            border: 1px solid rgba(185,154,101,0.65) !important;
            border-radius: 12px !important;

            background-color: #10283b !important;
            background-repeat: no-repeat !important;
            background-size: auto 100% !important;
            background-position: left center !important; 

            box-shadow: 0 10px 24px rgba(13,36,52,.17) !important;
            text-align: left !important;
            overflow: hidden !important;
            white-space: normal !important;
            transition: transform .2s, box-shadow .2s !important;
        }

        .st-key-home_diseno button {
            background-color: #284653 !important;
        }

        /* Integración suave entre ilustración y fondo */

        .st-key-home_oficios button::before,
        .st-key-home_diseno button::before {
            content: "";
            position: absolute;
            z-index: 0;
            inset: 0;
            pointer-events: none;

            background: linear-gradient(
                90deg,
                rgba(7, 20, 34, 0.10) 0%,
                rgba(12, 36, 54, 0.15) 38%,
                rgba(12, 36, 54, 0.90) 68%,
                rgba(12, 36, 54, 0.98) 100%
            );
        }

        .st-key-home_diseno button::before {
            background: linear-gradient(
                90deg,
                rgba(7, 20, 34, 0.08) 0%,
                rgba(33, 67, 79, 0.15) 38%,
                rgba(33, 67, 79, 0.90) 68%,
                rgba(33, 67, 79, 0.98) 100%
            );
        }

        /* Títulos */
        .st-key-home_oficios button p,
        .st-key-home_diseno button p {
            position: relative !important;
            z-index: 1 !important;

            width: 100% !important;
            margin: 0 !important;

            color: #ffffff !important;
            font-size: clamp(14px, 1.15vw, 17px) !important;
            font-weight: 800 !important;
            line-height: 1.35 !important;
            text-align: left !important;
            white-space: normal !important;
        }

        /* Descripciones */
        .st-key-home_oficios button::after,
        .st-key-home_diseno button::after {
            position: relative;
            z-index: 1;
            display: block;
            width: 100%;
            margin-top: 10px;
            font-size: 11px;
            line-height: 1.5;
            font-weight: 400;
            color: #d8e2e8;
            text-align: left;
        }

        .st-key-home_oficios button::after {
            content: "Consulta, registro y seguimiento documental";
        }

        .st-key-home_diseno button::after {
            content: "Fase I · Documentación técnica del proyecto";
        }

        /* Interacción */
        .st-key-home_oficios button:hover,
        .st-key-home_diseno button:hover {
            transform: translateY(-3px) !important;
            box-shadow: 0 16px 30px rgba(13,36,52,.23) !important;
            border-color: #d3ad67 !important;
        }

        /* Pie de página */
        .home-footer {
            text-align: center;
            color: #526778;
            font-size: 12px;
            margin-top: 30px;
            letter-spacing: 1px;
        }

        /* Pantallas pequeñas */
        @media (max-width: 760px) {
            .home-hero {
                padding: 10px 0 18px;
            }

            .st-key-home_oficios button,
            .st-key-home_diseno button {
                height: 180px !important;
                min-height: 180px !important;
                padding: 14px 12px 14px 50% !important;
                background-size: auto 100% !important;
            }

            .st-key-home_oficios button p,
            .st-key-home_diseno button p {
                font-size: 13px !important;
            }
        }
        </style>
        <section class="home-hero">
        <div class="home-kicker">MESA DE DISEÑO GEOMÉTRICO DE VÍA FÉRREA</div>
        <div class="home-title">CONTROL</div>
        <div class="home-subtitle">DOCUMENTAL</div>
        <div class="home-line"></div>
        </section>
        """,
    )

    estilos_imagenes = "<style>"
    if imagen_oficios:
        estilos_imagenes += (
            '.st-key-home_oficios button {'
            f'background-image: url("{imagen_oficios}") !important;'
            '}'
        )
    if imagen_ingenieria:
        estilos_imagenes += (
            '.st-key-home_diseno button {'
            f'background-image: url("{imagen_ingenieria}") !important;'
            '}'
        )
    estilos_imagenes += "</style>"
    st.html(estilos_imagenes)

    st.markdown(
        '<div class="home-module-label">SELECCIONA UN MÓDULO</div>',
        unsafe_allow_html=True,
    )
    col_oficios, col_diseno = st.columns([1, 1], gap="medium")
    with col_oficios:
        if st.button(
            "OFICIOS EN TRÁMITE", key="home_oficios", use_container_width=True
        ):
            navegar_a("oficios")
    with col_diseno:
        if st.button(
            "INGENIERÍA DE DETALLE", key="home_diseno", use_container_width=True
        ):
            navegar_a("diseno")
    st.markdown(
        '<div class="home-footer">TRAZO &nbsp;·&nbsp; ALINEAMIENTO &nbsp;·&nbsp; RASANTE</div>',
        unsafe_allow_html=True,
    )

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

    st.markdown("""
    <style>
    /* Solo Mis pendientes: conserva la identidad visual institucional. */
    .stMainBlockContainer:has(.pend-hero) {
        max-width: 1240px !important;
        padding-top: 2rem !important;
        padding-bottom: 4rem !important;
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

    </style>
    """, unsafe_allow_html=True)

    st.markdown("""
    <style>
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
    </style>
    """, unsafe_allow_html=True)
    # Ajustes visuales de Mis pendientes: no modifican el flujo ni las credenciales.
    st.markdown("""
    <style>
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
    </style>
    """, unsafe_allow_html=True)
    # Acabado visual del seguimiento: gris grafito, borde dorado y jerarquía compacta.
    # Solo afecta a las tarjetas documentales de este módulo.
    st.markdown("""
    <style>
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
    </style>
    """, unsafe_allow_html=True)
    # Acabado final: mismo lenguaje visual que Oficios en trámite.
    # Estas reglas están deliberadamente al final para prevalecer sobre el CSS previo.
    st.markdown("""
    <style>
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
    </style>
    """,unsafe_allow_html=True)
    # Corrección puntual de contraste: selectores directos que sí coinciden
    # con los contenedores reales de Streamlit (sin depender de :has()).
    st.markdown("""<style>
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
    </style>""",unsafe_allow_html=True)
    mostrar_mis_pendientes()

    # Ajustes visuales finales de Mis pendientes y Centro de autorizaciones.
    # Se mantienen intactos los procesos, permisos, versiones y conexiones.
    # Los fondos se cargan desde assets/ mediante el código existente.
    st.markdown("""
    <style>
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
    </style>
    """, unsafe_allow_html=True)


    # Paleta integral exclusiva de Mis pendientes. Se inyecta al final para
    # sustituir estilos azules de componentes y formularios sin tocar lógica.
    st.markdown("""
    <style>
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
    </style>
    """, unsafe_allow_html=True)

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
                background-color: #283E50 !important;
                color: white !important;
                border: none !important;
                border-radius: 6px !important;
                font-weight: 500;
            }}

            [data-testid="stSidebar"] .stButton button:hover {{
                background-color: #40576B !important;
                color: white !important;
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
                min-height: 178px;
                padding: 30px 38px;
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
                        CONTROL DOCUMENTAL / MESA DE DISEÑO GEOMÉTRICO
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
        """,
        height=200,
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
            margin-top: 4px;
            margin-bottom: 12px;
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
            height: 122px;
            padding: 17px 19px;
            background: #FFFFFF;
            border: 1px solid #DFE5E9;
            border-radius: 12px;
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

        .revision::before {
            background: #C6A36D;
        }

        .firma::before {
            background: #6887A0;
        }

        .concluidos::before {
            background: #4F927B;
        }

        .pendientes::before {
            background: #B77D61;
        }

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
                    <div class="icono">R</div>
                </div>
                <div class="nombre">En revisión</div>
                <div class="detalle">Documentación en análisis</div>
            </div>

            <div class="kpi firma">
                <div class="kpi-top">
                    <div class="numero">__FIRMA__</div>
                    <div class="icono">F</div>
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

            <div class="kpi pendientes">
                <div class="kpi-top">
                    <div class="numero">__PENDIENTES__</div>
                    <div class="icono">!</div>
                </div>
                <div class="nombre">Pendientes</div>
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
        height=145,
        scrolling=False
    )

    # =========================================================
    # CONSULTA DE EXPEDIENTES
    # =========================================================

    st.markdown(
        textwrap.dedent("""
        <div class="consulta-panel">
            <div class="consulta-encabezado">
                <div class="consulta-icono">⌕</div>
                <div>
                    <div class="consulta-titulo">Consulta de expedientes</div>
                    <div class="consulta-subtitulo">
                        Localiza un oficio por folio, número de oficio o asunto
                    </div>
                </div>
            </div>
        </div>
        """),
        unsafe_allow_html=True
    )


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
            placeholder="Folio, número de oficio o palabras del asunto...",
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

    st.write("")


    # =========================================================
    # BUSCAR RESULTADO
    # =========================================================

    oficio_encontrado = None

    if consulta.strip() and not df_oficios.empty:
        consulta_normalizada = consulta.strip().lower()

        for _, fila in df_oficios.iterrows():
            folio_busqueda = str(
                fila.get("Folio", "")
            ).strip().lower()

            numero_busqueda = str(
                fila.get("No. de oficio", "")
            ).strip().lower()

            asunto_busqueda = str(
                fila.get("Asunto", "")
            ).strip().lower()

            if (
                consulta_normalizada in folio_busqueda
                or consulta_normalizada in numero_busqueda
                or consulta_normalizada in asunto_busqueda
            ):
                oficio_encontrado = fila
                break

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

        st.markdown("""
        <style>
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
        </style>
        """, unsafe_allow_html=True)

        # Presentación compacta: solo modifica la apariencia de las celdas editables.
        st.markdown("""
        <style>
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
        </style>
        """, unsafe_allow_html=True)

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
            autorizado = puede_editar_responsable(responsable_resultado)
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
                        opciones = ["Alejandra", "Alex", "Jesús", "Cecilia", "Tte. Barajas",
                                    "Ricardo", "Magali", "John", "Karina"]
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

        st.markdown(f"""
        <div class="exp-titulo"><strong>Detalle del expediente</strong>
          <p>Información general y seguimiento del oficio seleccionado</p></div>
        <div class="exp-panel" style="border-bottom:0;border-radius:10px 10px 0 0">
          <div class="exp-cabecera"><div class="exp-cabecera-fila">
            <div><div class="exp-eyebrow">EXPEDIENTE LOCALIZADO</div>
              <div class="exp-numero">{dato_visible(numero_resultado or folio_resultado)}</div>
              <div class="exp-proyecto">{dato_visible(proyecto_resultado, 'Proyecto no especificado')}</div>
            </div><div class="exp-estado">{dato_visible(estatus_resultado, 'Sin estatus')}</div>
          </div></div>
        </div>
        """, unsafe_allow_html=True)

        if es_administrador():
            with st.expander("🗑 Eliminar este oficio · Confirmación obligatoria", expanded=False):
                _panel_eliminar_oficio(folio_resultado, numero_resultado, origen="busqueda")

        if not puede_editar_responsable(responsable_resultado):
            if es_administrador():
                st.error("No se pudo habilitar la edición. Vuelve a iniciar sesión y verifica el rol configurado.")
            else:
                st.info(
                    "Consulta de solo lectura: este oficio pertenece a "
                    f"{responsable_resultado or 'un responsable sin identificar'}. "
                    "Para editarlo debes tenerlo asignado en tu cuenta o acceder con un rol administrador."
                )
        else:
            st.caption("Para editar un dato, utiliza el icono ✎ junto al nombre del campo.")

        with st.container(key="expediente_campos"):
            for inicio_fila in (0, 3):
                columnas = st.columns(3, gap="small")
                for columna_st, campo in zip(columnas, campos[inicio_fila:inicio_fila + 3]):
                    with columna_st:
                        editar_campo(*campo)

        st.markdown(f"""
        <div class="exp-panel" style="border-radius:0 0 10px 10px;border-top:0;margin-top:0px">
          <div class="exp-seguimiento"><div class="exp-seguimiento-label">SEGUIMIENTO DEL TRÁMITE</div>
            <div class="exp-etapas">{etapas_html}</div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        # =====================================================
        # DOCUMENTOS DEL EXPEDIENTE
        # =====================================================

        antecedentes_resultado = leer_documentos(
            oficio_encontrado.get("Antecedente", "")
        )

        anexos_resultado = leer_documentos(
            oficio_encontrado.get("Anexo", "")
        )

        firmado_resultado = leer_documentos(
            oficio_encontrado.get("Firmado", "")
        )

        st.markdown(
            """<div style="border-top:1px solid #E1E7EA; padding-top:13px; margin-top:5px; margin-bottom:9px;"><div style="color:#10283B; font-size:13px; font-weight:750;">◇ &nbsp; Documentación vinculada</div><div style="color:#87959F; font-size:9px; margin-left:23px; margin-top:2px;">Consulta los documentos relacionados con este oficio</div></div>""",
            unsafe_allow_html=True
        )

        col_doc1, col_doc2, col_doc3 = st.columns(3)

        with col_doc1:
            with st.expander(f"▤  Antecedentes ({len(antecedentes_resultado)})"):
                if antecedentes_resultado:
                    for indice, documento in enumerate(antecedentes_resultado, start=1):
                        st.link_button(
                            documento["nombre"],
                            documento["url"],
                            use_container_width=True,
                        )
                else:
                    st.caption("Sin antecedentes registrados")

        with col_doc2:
            with st.expander(f"◇  Anexos ({len(anexos_resultado)})"):
                if anexos_resultado:
                    for indice, documento in enumerate(anexos_resultado, start=1):
                        st.link_button(
                            documento["nombre"],
                            documento["url"],
                            use_container_width=True,
                        )
                else:
                    st.caption("Sin anexos registrados")

        with col_doc3:
            with st.expander(f"▣  Oficio firmado ({len(firmado_resultado)})"):
                if firmado_resultado:
                    for indice, documento in enumerate(firmado_resultado, start=1):
                        st.link_button(
                            documento["nombre"],
                            documento["url"],
                            use_container_width=True,
                        )
                else:
                    st.caption("Sin oficio firmado registrado")

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
                if es_administrador():
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
                    st.info("Seleccionaste un oficio. La eliminación requiere iniciar sesión "
                            "con una cuenta de administrador o propietario.")
            elif len(oficios_seleccionados) > 1:
                st.info("Selecciona solo un oficio para habilitar la eliminación.")
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

elif st.session_state.menu_activo == "diseno":
    if st.button("← Volver al Menú Principal", key="volver_diseno"):
        st.session_state.menu_activo = "home"
        st.rerun()

    st.markdown("---")
    st.markdown("## Ingeniería de Detalle Fase I")
    st.info("Espacio preparado para la gestión de proyectos y control de esta mesa.")
