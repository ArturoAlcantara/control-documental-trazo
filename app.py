import base64
import json
import os
from pathlib import Path
import textwrap
import unicodedata
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import gspread
from google.oauth2.service_account import Credentials


# =========================================================
# CONFIGURACIÓN
# =========================================================

st.set_page_config(
    page_title="Control Documental",
    page_icon="📐",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# =========================================================
# FUNCIÓN PARA APLICAR EL FONDO
# =========================================================

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
                datos_usuario = st.secrets["usuarios"][usuario.lower()]
                if contrasena == datos_usuario["password"]:
                    st.session_state.autenticado = True
                    st.session_state.usuario_actual = datos_usuario["nombre"]
                    st.session_state.menu_activo = "home"
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")
            except KeyError:
                st.error("Usuario o contraseña incorrectos.")


def mostrar_sesion():
    col_sesion1, col_sesion2 = st.columns([6, 1])
    with col_sesion2:
        st.caption(
            f"Sesión: {st.session_state.usuario_actual}"
        )
        if st.button(
            "Cerrar sesión",
            key=f"cerrar_sesion_{st.session_state.menu_activo}",
            use_container_width=True
        ):
            st.session_state.autenticado = False
            st.session_state.usuario_actual = None
            st.session_state.menu_activo = "home"
            st.rerun()


# =========================================================
# BLOQUEO DE ACCESO
# =========================================================

if not st.session_state.autenticado:
    mostrar_login()
    st.stop()


if st.session_state.menu_activo == "home":
    aplicar_fondo()

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"


# =========================================================
# CONEXIÓN A GOOGLE SHEETS (HÍBRIDA: LOCAL + SECRETS)
# =========================================================

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
    hoja = conectar_google_sheets()
    registros = hoja.get_all_records()

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
    try:
        hoja = conectar_google_sheets()
        registros = hoja.get_all_records()
        return pd.DataFrame(registros)
    except Exception:
        return pd.DataFrame()


def guardar_oficio(datos):
    hoja = conectar_google_sheets()
    hoja.append_row(
        datos,
        value_input_option="USER_ENTERED"
    )


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

    # =====================================================
    # DATOS GENERALES
    # =====================================================

    st.markdown("#### Datos generales")

    col1, col2 = st.columns(2, gap="medium")

    with col1:
        folio = st.text_input(
            "Folio",
            key=f"folio_{version}"
        )

        # =====================================================
        # PROGRAMA Y PROYECTO
        # =====================================================

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

        fase = None
        proyecto_seleccionado = None

        # -----------------------------------------------------
        # TREN MAYA
        # -----------------------------------------------------

        if programa == "Tren Maya":

            proyecto_seleccionado = st.selectbox(
                "Proyecto",
                [
                    "Terminales de Carga",
                    "Bases de Mantenimiento",
                    "Vialidades de Acceso a las Edificaciones"
                ],
                index=None,
                placeholder="Seleccionar proyecto",
                key=f"proyecto_maya_{version}"
            )

        # -----------------------------------------------------
        # TRENES DEL NORTE
        # -----------------------------------------------------

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

        # -----------------------------------------------------
        # PROYECTO NUEVO / PERSONALIZADO
        # -----------------------------------------------------

        proyecto_nuevo = st.text_input(
            "Proyecto nuevo (opcional)",
            placeholder="Escribe aquí si el proyecto no aparece en la lista...",
            key=f"proyecto_nuevo_{version}"
        )

        # -----------------------------------------------------
        # CONSTRUIR NOMBRE FINAL DEL PROYECTO
        # -----------------------------------------------------

        if proyecto_nuevo.strip():

            # Si se escribió un proyecto nuevo, tiene prioridad
            proyecto = proyecto_nuevo.strip()

        elif programa == "Tren Maya" and proyecto_seleccionado:

            proyecto = (
                f"Tren Maya - {proyecto_seleccionado}"
            )

        elif (
            programa == "Trenes del Norte"
            and fase
            and proyecto_seleccionado
        ):

            proyecto = (
                f"Trenes del Norte - "
                f"{fase} - "
                f"{proyecto_seleccionado}"
            )

        else:
            proyecto = ""

    with col2:
        numero = st.text_input(
            "No. de oficio",
            key=f"numero_{version}"
        )

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

    asunto = st.text_area(
        "Asunto",
        height=75,
        key=f"asunto_{version}"
    )

    st.divider()

    # =====================================================
    # SEGUIMIENTO DEL TRÁMITE
    # =====================================================

    st.markdown("#### Seguimiento del trámite")

    col3, col4 = st.columns(2, gap="medium")

    with col3:
        estatus = st.selectbox(
            "Estatus",
            [
                "En revisión",
                "En espera de firma",
                "Recaído",
                "Concluido"
            ],
            key=f"estatus_{version}"
        )

        fecha_entrega = st.date_input(
            "Fecha de entrega",
            value=None,
            key=f"entrega_{version}"
        )

    with col4:
        fecha_recepcion = st.date_input(
            "Fecha de recepción",
            value=None,
            key=f"recepcion_{version}"
        )

        firmado = st.text_input(
            "Oficio firmado · Enlace Drive",
            key=f"firmado_{version}"
        )

    st.divider()

    # =====================================================
    # DOCUMENTACIÓN ADJUNTA
    # =====================================================

    st.markdown("#### Documentación adjunta")

    col5, col6 = st.columns(2, gap="medium")

    with col5:
        st.markdown("**Antecedentes**")
        antecedentes = documentos_formulario(
            f"antecedentes_{version}"
        )

    with col6:
        st.markdown("**Anexos**")
        anexos = documentos_formulario(
            f"anexos_{version}"
        )

    observaciones = st.text_area(
        "Observaciones",
        height=75,
        key=f"observaciones_{version}"
    )

    st.divider()

    # =====================================================
    # GUARDAR OFICIO
    # =====================================================

    if st.button(
        "Guardar oficio",
        type="primary",
        use_container_width=True,
        key=f"guardar_oficio_{version}"
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

                # Preparar información (asegurando que el proyecto use el valor seleccionado)
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
                        if fecha_entrega else "",
                    json.dumps(
                        antecedentes,
                        ensure_ascii=False
                    ),
                    json.dumps(
                        anexos,
                        ensure_ascii=False
                    ),
                    firmado.strip(),
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

    if firmado:
        st.link_button(
            "Ver oficio firmado",
            firmado
        )

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
    background-image: url("data:image/jpeg;base64,""" + obtener_imagen_base64("fondo_planos.jpg") + """");
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
    padding-top: 8vh !important;
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

/* Botones invisibles únicamente del menú principal */
.st-key-btn_oficios div[data-testid="stButton"],
.st-key-btn_diseno div[data-testid="stButton"] {
    width: 100%;
    margin-top: -145px;
    position: relative;
    z-index: 10;
}

.st-key-btn_oficios button,
.st-key-btn_diseno button {
    width: 100% !important;
    height: 145px !important;
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    padding: 0 !important;
    opacity: 0;
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

</style>
""",
    unsafe_allow_html=True,
)


# =========================================================
# HOME
# =========================================================

if st.session_state.menu_activo == "home":

    # -----------------------------------------------------
    # SESIÓN DEL USUARIO
    # -----------------------------------------------------
    mostrar_sesion()

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

    # -----------------------------------------------------
    # SESIÓN DEL USUARIO
    # -----------------------------------------------------
    mostrar_sesion()

    # Fondo exclusivo del módulo Oficios y del menú lateral
    fondo_oficios = cargar_imagen_base64(
        "assets/fondo_oficios.png"
    )
    if fondo_oficios:
        st.markdown(
            f"""
            <style>
            .stApp {{
                background-image: url("data:image/png;base64,{fondo_oficios}");
                background-size: cover;
                background-position: center center;
                background-repeat: no-repeat;
                background-attachment: fixed;
            }}

            [data-testid="stSidebar"] {{
                background-image: url("data:image/png;base64,{fondo_oficios}") !important;
                background-size: cover !important;
                background-position: left center !important;
                background-repeat: no-repeat !important;
                border-right: 1px solid #D8D5CC !important;
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

    # ============================================================
    # MENÚ LATERAL - MESA DE DISEÑO GEOMÉTRICO
    # ============================================================

    with st.sidebar:
        st.markdown(
            """
            <style>
            .logo-mesa {
                width: 155px;
                height: 155px;
                margin: -15px auto 12px auto;
                padding: 8px;
                background-color: #FFFFFF;
                border: 2px solid #C6AC79;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                box-sizing: border-box;
            }

            .logo-mesa img {
                width: 100%;
                height: 100%;
                object-fit: contain;
            }
            </style>
            """,
            unsafe_allow_html=True
        )

        logo_base64 = cargar_imagen_base64(
            "assets/logo_diseno_geometrico.png"
        )

        if logo_base64:
            st.markdown(
                f"""
                <div class="logo-mesa">
                    <img src="data:image/png;base64,{logo_base64}">
                </div>
                """,
                unsafe_allow_html=True
            )

        # Identificación de la mesa
        st.markdown(
            """
            <div style="text-align:center;">
                <h3 style="
                    color:#283E50;
                    font-size:17px;
                    font-weight:700;
                    margin-bottom:6px;
                ">
                    CONTROL DOCUMENTAL
                </h3>
                <p style="
                    color:#687782;
                    font-size:13px;
                    margin-top:0;
                ">
                    Mesa de Diseño Geométrico
                </p>
            </div>
            """,
            unsafe_allow_html=True
        )

        st.markdown("<br>", unsafe_allow_html=True)

        if st.button(
            "⌂ Menú principal",
            key="sidebar_volver_oficios",
            use_container_width=True
        ):
            st.session_state.menu_activo = "home"
            st.rerun()

        st.divider()

    # ============================================================
    # 1. ENCABEZADO INSTITUCIONAL CON ESQUEMA FERROVIARIO
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
                font-family: Arial, sans-serif;
                background: transparent;
            }

            .encabezado-oficios {
                background-color: #343F46;
                border-left: 5px solid #C6A36D;
                border-radius: 10px;
                padding: 25px 30px;
                width: 100%;
                min-height: 160px;

                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 25px;
            }

            .encabezado-texto {
                flex: 1;
                min-width: 0;
            }

            .etiqueta {
                color: #C6B18D;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 2px;
            }

            .titulo {
                color: #FFFFFF;
                font-size: 32px;
                font-weight: 700;
                margin: 14px 0 0 0;
                letter-spacing: 0.5px;
            }

            .linea-dorada {
                background-color: #C6A36D;
                width: 90px;
                height: 3px;
                margin: 12px 0;
            }

            .subtitulo {
                color: #D2D9DD;
                font-size: 14px;
                margin: 0;
            }

            .esquema-ferroviario {
                width: 35%;
                max-width: 350px;
                min-width: 200px;
                opacity: 0.85;
            }

            .esquema-ferroviario svg {
                width: 100%;
                height: auto;
                display: block;
            }

            @media (max-width: 650px) {
                .encabezado-oficios {
                    padding: 22px;
                }

                .titulo {
                    font-size: 24px;
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
                        CONTROL DOCUMENTAL
                    </span>

                    <h1 class="titulo">
                        OFICIOS EN TRÁMITE
                    </h1>

                    <div class="linea-dorada"></div>

                    <p class="subtitulo">
                        Mesa de Diseño Geométrico · Seguimiento de oficios
                    </p>

                </div>

                <div class="esquema-ferroviario">

                    <svg viewBox="0 0 330 120"
                         xmlns="http://www.w3.org/2000/svg">

                        <!-- Cuadrícula técnica -->
                        <g stroke="#68777B"
                           stroke-width="0.5"
                           opacity="0.35">

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

                        <!-- Alineamiento ferroviario -->
                        <path
                            d="M-10 100 C80 100 100 25 190 35 S280 85 340 20"
                            stroke="#C6A36D"
                            stroke-width="2.5"
                            fill="none"
                        />

                        <!-- Línea paralela -->
                        <path
                            d="M-10 111 C80 111 100 36 190 46 S280 96 340 31"
                            stroke="#A6B5BC"
                            stroke-width="1.5"
                            fill="none"
                        />

                        <!-- Trazo auxiliar -->
                        <path
                            d="M10 90 L48 80 L86 55 L130 30 L180 26 L230 43 L285 48"
                            stroke="#A6B5BC"
                            stroke-width="0.8"
                            stroke-dasharray="4 5"
                            fill="none"
                            opacity="0.7"
                        />

                        <!-- Puntos de control -->
                        <g fill="#343F46"
                           stroke="#C6A36D"
                           stroke-width="1.5">

                            <circle cx="48" cy="80" r="3"/>
                            <circle cx="130" cy="30" r="3"/>
                            <circle cx="230" cy="43" r="3"/>

                        </g>

                        <!-- Identificador técnico -->
                        <text
                            x="185"
                            y="112"
                            font-size="8"
                            fill="#B5C0C5"
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
        height=190,
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
        n_concluidos = (estatus_series == "concluido").sum()
        n_pendientes = (estatus_series != "concluido").sum()

    st.markdown(
        """
        <style>

        /* =====================================================
            CABECERA DE SECCIÓN
        ===================================================== */

        .of-panel-header {
            margin-top: 18px;
            margin-bottom: 18px;
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
    # INDICADORES (VIA COMPONENTS.HTML)
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
    font-family: Arial, sans-serif;
}

.kpi-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    width: 100%;
}

.kpi {
    position: relative;
    overflow: hidden;
    height: 125px;
    padding: 18px 20px;

    background: linear-gradient(
        135deg,
        #28586A 0%,
        #347084 100%
    );

    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.15);

    box-shadow:
        0 8px 18px rgba(16,40,59,0.10);
}

.kpi::before {
    content: "";
    position: absolute;

    left: 0;
    top: 0;
    bottom: 0;

    width: 4px;

    background: #B08A4A;
}

.kpi::after {
    content: "";
    position: absolute;

    width: 170px;
    height: 60px;

    right: -25px;
    bottom: -31px;

    border-top: 2px solid rgba(207,170,101,0.55);
    border-radius: 50%;

    transform: rotate(-7deg);
}

.codigo {
    position: absolute;

    right: 16px;
    top: 14px;

    color: rgba(255,255,255,0.25);

    font-size: 9px;
    font-weight: 700;

    letter-spacing: 1.5px;
}

.numero {
    position: relative;
    z-index: 2;

    color: #FFFFFF;

    font-size: 35px;
    font-weight: 700;

    line-height: 1;

    margin-bottom: 11px;
}

.nombre {
    position: relative;
    z-index: 2;

    color: #D2AE69;

    font-size: 10px;
    font-weight: 700;

    letter-spacing: 1.3px;

    text-transform: uppercase;
}

.detalle {
    position: relative;
    z-index: 2;

    color: #B8C2C8;

    font-size: 10px;

    margin-top: 6px;
}

</style>

</head>

<body>

<div class="kpi-grid">

    <div class="kpi">
        <div class="codigo">REV</div>
        <div class="numero">__REVISION__</div>
        <div class="nombre">En revisión</div>
        <div class="detalle">Documentación en análisis</div>
    </div>

    <div class="kpi">
        <div class="codigo">FIR</div>
        <div class="numero">__FIRMA__</div>
        <div class="nombre">En firma</div>
        <div class="detalle">Pendiente de formalización</div>
    </div>

    <div class="kpi">
        <div class="codigo">CON</div>
        <div class="numero">__CONCLUIDOS__</div>
        <div class="nombre">Concluidos</div>
        <div class="detalle">Expedientes finalizados</div>
    </div>

    <div class="kpi">
        <div class="codigo">PEN</div>
        <div class="numero">__PENDIENTES__</div>
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

    st.markdown("<br>", unsafe_allow_html=True)


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


        # =====================================================
        # TÍTULO DEL PANEL
        # =====================================================

        st.markdown(
            """
<div style="margin-top:18px; margin-bottom:10px;">
    <div style="color:#10283B; font-size:17px; font-weight:750;">
        ▣ &nbsp; Detalle del expediente
    </div>
    <div style="color:#81909B; font-size:10px; margin-left:27px; margin-top:2px;">
        Información general del oficio seleccionado
    </div>
</div>
""",
            unsafe_allow_html=True
        )


        # =====================================================
        # EXPEDIENTE LOCALIZADO
        # =====================================================

        st.markdown(
            f"""<div class="of-record" style="padding:18px 22px;"><div style="display:flex; justify-content:space-between; align-items:center; gap:20px;"><div><div class="of-record-label">EXPEDIENTE LOCALIZADO</div><div class="of-record-title">{numero_resultado or folio_resultado}</div><div class="of-record-project">{proyecto_resultado or "Proyecto no especificado"}</div></div><div style="flex-shrink:0; padding:7px 15px; border-radius:20px; background:rgba(176,138,74,0.25); border:1px solid rgba(213,174,96,0.70); color:#FFFFFF; font-size:10px; font-weight:600;">◷ &nbsp; {estatus_resultado or "Sin estatus"}</div></div></div>""",
            unsafe_allow_html=True
        )


        # =====================================================
        # INFORMACIÓN GENERAL — 3 COLUMNAS
        # =====================================================

        col_dato1, col_dato2, col_dato3 = st.columns(3)

        with col_dato1:
            st.markdown(
                f"""
<div style="padding:10px 8px;">
    <div style="color:#82919B; font-size:9px; font-weight:700; letter-spacing:1px;">
        ▤ &nbsp; FOLIO
    </div>
    <div style="color:#10283B; font-size:13px; font-weight:650; margin-top:4px;">
        {folio_resultado or "—"}
    </div>
</div>
""",
                unsafe_allow_html=True
            )

        with col_dato2:
            st.markdown(
                f"""
<div style="padding:10px 8px; border-left:1px solid #E3E8EB;">
    <div style="color:#82919B; font-size:9px; font-weight:700; letter-spacing:1px;">
        ♙ &nbsp; RESPONSABLE
    </div>
    <div style="color:#10283B; font-size:13px; font-weight:650; margin-top:4px;">
        {responsable_resultado or "—"}
    </div>
</div>
""",
                unsafe_allow_html=True
            )

        with col_dato3:
            st.markdown(
                f"""
<div style="padding:10px 8px; border-left:1px solid #E3E8EB;">
    <div style="color:#82919B; font-size:9px; font-weight:700; letter-spacing:1px;">
        ⚙ &nbsp; ESTATUS
    </div>
    <div style="color:#10283B; font-size:13px; font-weight:650; margin-top:4px;">
        {estatus_resultado or "—"}
    </div>
</div>
""",
                unsafe_allow_html=True
            )


        # =====================================================
        # SEGUNDA FILA
        # =====================================================

        col_dato4, col_dato5, col_dato6 = st.columns(3)

        with col_dato4:
            st.markdown(
                f"""
<div style="padding:10px 8px;">
    <div style="color:#82919B; font-size:9px; font-weight:700; letter-spacing:1px;">
        ▦ &nbsp; FECHA DE RECEPCIÓN
    </div>
    <div style="color:#10283B; font-size:13px; font-weight:650; margin-top:4px;">
        {fecha_resultado or "—"}
    </div>
</div>
""",
                unsafe_allow_html=True
            )

        with col_dato5:
            st.markdown(
                f"""
<div style="padding:10px 8px; border-left:1px solid #E3E8EB;">
    <div style="color:#82919B; font-size:9px; font-weight:700; letter-spacing:1px;">
        ◇ &nbsp; PROYECTO
    </div>
    <div style="color:#10283B; font-size:13px; font-weight:650; margin-top:4px;">
        {proyecto_resultado or "—"}
    </div>
</div>
""",
                unsafe_allow_html=True
            )

        with col_dato6:
            st.markdown(
                f"""
<div style="padding:10px 8px; border-left:1px solid #E3E8EB;">
    <div style="color:#82919B; font-size:9px; font-weight:700; letter-spacing:1px;">
        ▣ &nbsp; ASUNTO
    </div>
    <div style="color:#10283B; font-size:12px; font-weight:650; margin-top:4px; line-height:1.35;">
        {asunto_resultado or "—"}
    </div>
</div>
""",
                unsafe_allow_html=True
            )


        # =====================================================
        # SEGUIMIENTO VISUAL DEL OFICIO
        # =====================================================

        estatus_normalizado = estatus_resultado.strip().lower()

        # Determinar la etapa actual
        if estatus_normalizado in [
            "firmado",
            "concluido",
            "finalizado"
        ]:
            etapa_actual = 4

        elif estatus_normalizado in [
            "en proceso de firma",
            "en firma",
            "en espera de firma"
        ]:
            etapa_actual = 3

        elif estatus_normalizado in [
            "en revisión",
            "en revision",
            "en proceso"
        ]:
            etapa_actual = 2

        else:
            etapa_actual = 1


        # Crear visualmente cada etapa
        def crear_etapa(numero, icono, titulo, descripcion):

            # La etapa YA fue superada
            if numero < etapa_actual:
                fondo = "#1F7A5A"
                borde = "#1F7A5A"
                color_icono = "#FFFFFF"
                contenido = "✓"
                color_titulo = "#10283B"

            # Es la etapa ACTUAL
            elif numero == etapa_actual:

                # Firmado es la única etapa actual que muestra ✓
                if numero == 4:
                    fondo = "#1F7A5A"
                    borde = "#1F7A5A"
                    color_icono = "#FFFFFF"
                    contenido = "✓"

                else:
                    fondo = "#FFFFFF"
                    borde = "#B08A4A"
                    color_icono = "#B08A4A"
                    contenido = icono

                color_titulo = "#10283B"

            # Todavía NO se llega a esta etapa
            else:
                fondo = "#E7ECEF"
                borde = "#E7ECEF"
                color_icono = "#9AA7AF"
                contenido = icono
                color_titulo = "#8997A0"

            return f"""<div style="text-align:center; position:relative; z-index:2;">
<div style="width:32px; height:32px; border-radius:50%; background:{fondo}; border:3px solid {borde}; color:{color_icono}; margin:0 auto 7px auto; display:flex; align-items:center; justify-content:center; font-size:14px; font-weight:800;">{contenido}</div>
<div style="font-size:11px; color:{color_titulo}; font-weight:750;">{titulo}</div>
<div style="font-size:8px; color:#8B99A3; margin-top:3px;">{descripcion}</div>
</div>"""


        # Las cuatro etapas
        etapa_1 = crear_etapa(
            1,
            "📅",
            "Fecha de recepción",
            fecha_resultado or "—"
        )

        etapa_2 = crear_etapa(
            2,
            "🔎",
            "En revisión",
            "Revisión del expediente"
        )

        etapa_3 = crear_etapa(
            3,
            "✍",
            "En proceso de firma",
            "Pendiente de formalización"
        )

        etapa_4 = crear_etapa(
            4,
            "✓",
            "Firmado",
            "Documento formalizado"
        )


        # Construir la línea de seguimiento
        seguimiento_html = f"""<div style="position:relative; margin-top:17px; margin-bottom:18px;">
<div style="position:absolute; top:15px; left:12%; right:12%; height:2px; background:#D5DDE2;"></div>
<div style="display:grid; grid-template-columns:repeat(4,1fr); position:relative;">
{etapa_1}
{etapa_2}
{etapa_3}
{etapa_4}
</div>
</div>"""

        st.markdown(
            seguimiento_html,
            unsafe_allow_html=True
        )


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
            if antecedentes_resultado:
                st.link_button(
                    "▤  Antecedentes  ›",
                    antecedentes_resultado[0]["url"],
                    use_container_width=True
                )
            else:
                st.button(
                    "— Sin antecedentes",
                    disabled=True,
                    use_container_width=True,
                    key="sin_antecedentes_consulta"
                )

        with col_doc2:
            if anexos_resultado:
                st.link_button(
                    "◇  Anexos  ›",
                    anexos_resultado[0]["url"],
                    use_container_width=True
                )
            else:
                st.button(
                    "— Sin anexos",
                    disabled=True,
                    use_container_width=True,
                    key="sin_anexos_consulta"
                )

        with col_doc3:
            if firmado_resultado:
                st.link_button(
                    "▣  Oficio firmado  ›",
                    firmado_resultado[0]["url"],
                    use_container_width=True
                )
            else:
                st.button(
                    "— Sin oficio firmado",
                    disabled=True,
                    use_container_width=True,
                    key="sin_firmado_consulta"
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
    st.markdown("<br>", unsafe_allow_html=True)
    st.divider()
    st.markdown(
        "<p style='text-align:center;color:#667784;font-size:11px;font-weight:600;letter-spacing:2px;'>ADMINISTRACIÓN DOCUMENTAL</p>",
        unsafe_allow_html=True
    )
    st.markdown("<br>", unsafe_allow_html=True)

    if "mostrar_registro" not in st.session_state:
        st.session_state.mostrar_registro = False

    if st.session_state.mostrar_registro:
        with st.container(border=True):
            formulario_nuevo_oficio()

    # =========================================================
    # TABLA GENERAL DE OFICIOS
    # =========================================================
    st.markdown("<br><br>", unsafe_allow_html=True)
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

        # -------------------------------------------------
        # PREPARAR COLUMNAS DOCUMENTALES
        # -------------------------------------------------

        if "Antecedente" in tabla_oficios.columns:
            tabla_oficios["Antecedente"] = tabla_oficios["Antecedente"].apply(
                lambda valor: (
                    leer_documentos(valor)[0]["url"]
                    if leer_documentos(valor)
                    else None
                )
            )

        if "Anexo" in tabla_oficios.columns:
            tabla_oficios["Anexo"] = tabla_oficios["Anexo"].apply(
                lambda valor: (
                    leer_documentos(valor)[0]["url"]
                    if leer_documentos(valor)
                    else None
                )
            )

        if "Firmado" in tabla_oficios.columns:
            tabla_oficios["Firmado"] = tabla_oficios["Firmado"].apply(
                lambda valor: (
                    leer_documentos(valor)[0]["url"]
                    if leer_documentos(valor)
                    else None
                )
            )

        # -------------------------------------------------
        # MOSTRAR TABLA
        # -------------------------------------------------

        st.dataframe(
            tabla_oficios,
            use_container_width=True,
            hide_index=True,
            height=420,
            column_config={
                "Folio": st.column_config.TextColumn(
                    "Folio",
                    width="small"
                ),
                "No. de oficio": st.column_config.TextColumn(
                    "No. de oficio",
                    width="medium"
                ),
                "Asunto": st.column_config.TextColumn(
                    "Asunto",
                    width="large"
                ),
                "Proyecto": st.column_config.TextColumn(
                    "Proyecto",
                    width="medium"
                ),
                "Responsable": st.column_config.TextColumn(
                    "Responsable",
                    width="medium"
                ),
                "Estatus": st.column_config.TextColumn(
                    "Estatus",
                    width="medium"
                ),
                "Antecedente": st.column_config.LinkColumn(
                    "Antecedente",
                    display_text="👁",
                    width="small"
                ),
                "Anexo": st.column_config.LinkColumn(
                    "Anexo",
                    display_text="👁",
                    width="small"
                ),
                "Firmado": st.column_config.LinkColumn(
                    "Firmado",
                    display_text="👁",
                    width="small"
                ),
                "Observaciones": st.column_config.TextColumn(
                    "Observaciones",
                    width="large"
                ),
            }
        )

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

    if st.button(
        "Entrar a Ingeniería de Detalle Fase I",
        key="btn_diseno",
        use_container_width=True
    ):
        st.session_state.menu_activo = "diseno"
        st.rerun()