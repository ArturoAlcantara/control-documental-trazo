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

if st.session_state.menu_activo == "home":
    aplicar_fondo()

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"


# =========================================================
# CONEXIÓN A GOOGLE SHEETS (LOCAL CON CREDENTIALS.JSON)
# =========================================================

def conectar_google_sheets():
    ruta = Path(__file__).parent / "credentials.json"
    with open(ruta, "r", encoding="utf-8") as archivo:
        credenciales_json = json.load(archivo)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
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


# ==========================================
# PRUEBA DE CONEXIÓN Y LECTURA DE REGISTROS
# ==========================================
try:
    hoja = conectar_google_sheets()
    registros = hoja.get_all_records()
    df_oficios = pd.DataFrame(registros)
except Exception as e:
    st.error("Error al conectar con Google Sheets")
    st.exception(e)


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

        proyecto = st.text_input(
            "Proyecto",
            key=f"proyecto_{version}"
        )

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

                # Preparar información
                datos = [
                    folio.strip(),
                    numero.strip(),
                    asunto.strip(),
                    proyecto.strip(),
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
    # 2. PANEL TÉCNICO DE CONTROL DOCUMENTAL
    # ============================================================

    st.markdown("""
    <style>
    /* PANEL PRINCIPAL */
    .st-key-panel_tecnico_oficios {
        background: #FFFFFF !important;
        border: 1px solid #DCE3E7 !important;
        border-left: 4px solid #B89A67 !important;
        border-radius: 12px !important;
        padding: 18px 22px !important;
    }

    /* SECCIÓN CONSULTA Y EXPEDIENTE */
    .seccion-titulo {
        color: #303D44;
        font-size: 17px;
        font-weight: 700;
        margin: 0 0 3px 0;
    }
    .seccion-subtitulo {
        color: #87919A;
        font-size: 12px;
        margin: 0 0 14px 0;
    }

    /* RESULTADO DE BÚSQUEDA */
    .caja-resultado {
        padding: 16px;
        border: 1px solid #DFE4E8;
        border-radius: 9px;
        background: #F9FAFB;
        color: #87919A;
        font-size: 13px;
        text-align: center;
        margin-top: 10px;
    }

    /* EXPEDIENTE DOCUMENTAL */
    .panel-expediente {
        background: #F7F9FA;
        border: 1px solid #E0E5E8;
        border-radius: 10px;
        padding: 18px;
    }

    /* BOTONES DOCUMENTALES (CUADRÍCULA 2x2) */
    .st-key-panel_expediente div.stButton > button {
        background: #FFFFFF !important;
        color: #33414B !important;
        border: 1px solid #DCE2E6 !important;
        border-radius: 8px !important;
        min-height: 48px !important;
        font-weight: 600 !important;
        font-size: 13px !important;
        width: 100% !important;
    }
    .st-key-panel_expediente div.stButton > button:hover {
        border-color: #B89A67 !important;
        background: #F4F0E8 !important;
    }
    .st-key-panel_expediente div.stButton > button:disabled {
        opacity: 0.65 !important;
        color: #64717B !important;
    }
    </style>
    """, unsafe_allow_html=True)

    with st.container(key="panel_tecnico_oficios"):

        # ========================================================
        # CARGA DE DATOS DE GOOGLE SHEETS PARA CÁLCULO DE MÉTRICAS
        # ========================================================
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

        # PANEL INSTITUCIONAL (ENCABEZADO + 4 INDICADORES DINÁMICOS) VÍA COMPONENTS.HTML
        html_tablero = textwrap.dedent("""
        <style>
        body {
            margin: 0;
            padding: 0;
            background: transparent;
            font-family: 'Arial', sans-serif;
        }
        .panel-institucional {
            width: 100%;
            padding: 2px 0 10px 0;
            text-align: center;
        }

        .panel-institucional .panel-etiqueta {
            color: #B08A4A;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 2px;
            margin-bottom: 6px;
        }

        .panel-institucional .panel-titulo {
            color: #263B4A;
            font-size: 22px;
            font-weight: 700;
            margin: 0 0 4px 0;
        }

        .panel-institucional .panel-subtitulo {
            color: #8795A3;
            font-size: 12px;
            margin: 0;
        }

        .panel-institucional .panel-linea {
            width: 100%;
            height: 1px;
            background: #DFE4E8;
            margin: 14px 0;
        }

        /* TABLERO INSTITUCIONAL */
        .panel-indicadores {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 18px;
            margin: 10px 0 0 0;
            width: 100%;
            box-sizing: border-box;
        }

        .panel-indicador {
            background: #F1F3F5;
            border: 1px solid #D7DFE5;
            border-radius: 9px;
            min-height: 125px;
            padding: 17px 10px;
            box-sizing: border-box;

            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
        }

        .panel-indicador-titulo {
            color: #657B8E;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
            text-transform: uppercase;
            line-height: 1.4;
            min-height: 31px;
        }

        .panel-indicador-numero {
            color: #253B4A;
            font-size: 32px;
            font-weight: 700;
            line-height: 1.2;
            margin-top: 8px;
        }

        .panel-indicador-acento {
            width: 30px;
            height: 3px;
            background: #B99557;
            margin-top: 10px;
        }

        @media (max-width: 650px) {
            .panel-indicadores {
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 12px;
            }
        }
        </style>

        <div class="panel-institucional">

            <div class="panel-etiqueta">
                GESTIÓN DOCUMENTAL / SEGUIMIENTO
            </div>

            <h2 class="panel-titulo">
                Panel de control de oficios
            </h2>

            <p class="panel-subtitulo">
                Mesa de Diseño Geométrico · Estado actual y consulta de expedientes
            </p>

            <div class="panel-linea"></div>

            <div class="panel-indicadores">

                <div class="panel-indicador">
                    <div class="panel-indicador-titulo">
                        En revisión
                    </div>
                    <div class="panel-indicador-numero">REV_VAL</div>
                    <div class="panel-indicador-acento"></div>
                </div>

                <div class="panel-indicador">
                    <div class="panel-indicador-titulo">
                        En firma
                    </div>
                    <div class="panel-indicador-numero">FIR_VAL</div>
                    <div class="panel-indicador-acento"></div>
                </div>

                <div class="panel-indicador">
                    <div class="panel-indicador-titulo">
                        Concluidos esta semana
                    </div>
                    <div class="panel-indicador-numero">CON_VAL</div>
                    <div class="panel-indicador-acento"></div>
                </div>

                <div class="panel-indicador">
                    <div class="panel-indicador-titulo">
                        Pendientes totales
                    </div>
                    <div class="panel-indicador-numero">PEN_VAL</div>
                    <div class="panel-indicador-acento"></div>
                </div>

            </div>

        </div>
        """)
        
        # Reemplazamos los marcadores de posición con los valores reales calculados
        html_tablero = html_tablero.replace("REV_VAL", str(n_revision))
        html_tablero = html_tablero.replace("FIR_VAL", str(n_firma))
        html_tablero = html_tablero.replace("CON_VAL", str(n_concluidos))
        html_tablero = html_tablero.replace("PEN_VAL", str(n_pendientes))

        components.html(html_tablero, height=205, scrolling=False)

        st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
        st.divider()
        st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)

        # CENTRO DE CONSULTA Y EXPEDIENTE DOCUMENTAL
        col_busqueda, col_expediente = st.columns([1.3, 1], gap="large")
        oficio_encontrado = None

        with col_busqueda:
            st.markdown("""
                <div class="seccion-titulo">Centro de consulta</div>
                <div class="seccion-subtitulo">Localización y seguimiento de expedientes</div>
            """, unsafe_allow_html=True)

            busqueda = st.text_input(
                "Folio, número de oficio o asunto",
                placeholder="Escribe el folio, número o asunto del oficio...",
                key="busqueda_tecnica_oficios"
            )

            if busqueda.strip():
                try:
                    resultados = buscar_oficios(busqueda)
                    if resultados:
                        st.success(f"Se encontraron {len(resultados)} oficio(s).")
                        if len(resultados) == 1:
                            oficio_encontrado = resultados[0]
                        else:
                            opciones = list(range(len(resultados)))
                            indice = st.selectbox(
                                "Selecciona el oficio que deseas consultar",
                                opciones,
                                format_func=lambda i: (
                                    f"{resultados[i].get('folio', '')} · "
                                    f"{resultados[i].get('no. de oficio', '')}"
                                ),
                                key="seleccion_oficio"
                            )
                            oficio_encontrado = resultados[indice]

                        oficio = oficio_encontrado
                        st.markdown("### Expediente localizado")
                        for etiqueta, columna in [
                            ("Folio", "folio"),
                            ("Número de oficio", "no. de oficio"),
                            ("Asunto", "asunto"),
                            ("Proyecto", "proyecto"),
                            ("Responsable", "responsable"),
                            ("Estatus", "estatus"),
                            ("Fecha de recepción", "fecha de recepcion"),
                            ("Fecha de entrega", "fecha de entrega"),
                            ("Observaciones", "observaciones"),
                        ]:
                            st.write(f"**{etiqueta}:**", oficio.get(columna, "") or "Pendiente")
                    else:
                        st.info("No se encontraron oficios con ese criterio.")
                except Exception as error:
                    st.error("No fue posible consultar los oficios en Google Sheets.")
                    st.caption(str(error))
            else:
                st.markdown("""
                    <div class="caja-resultado">
                        Ingresa un folio, número de oficio o asunto para consultar su información.
                    </div>
                """, unsafe_allow_html=True)

        with col_expediente:
            with st.container(key="panel_expediente"):
                st.markdown("""
                    <div class="seccion-titulo">Expediente documental</div>
                    <div class="seccion-subtitulo">Accesos directos del oficio seleccionado</div>
                """, unsafe_allow_html=True)

                if oficio_encontrado:
                    antecedentes = leer_documentos(oficio_encontrado.get("antecedente", ""))
                    anexos = leer_documentos(oficio_encontrado.get("anexo", ""))
                    firmado = leer_documentos(oficio_encontrado.get("firmado", ""))
                    responsable = str(oficio_encontrado.get("responsable", "")).strip()
                else:
                    antecedentes, anexos, firmado, responsable = [], [], [], ""

                # Limpiar la selección anterior cuando cambia el oficio consultado.
                identidad = (
                    str(oficio_encontrado.get("folio", "")),
                    str(oficio_encontrado.get("no. de oficio", "")),
                ) if oficio_encontrado else None
                if st.session_state.get("oficio_documentos_id") != identidad:
                    st.session_state.oficio_documentos_id = identidad
                    st.session_state.panel_documental_activo = None

                c1, c2 = st.columns(2, gap="small")
                with c1:
                    if st.button("↺ Antecedentes", key="btn_tec_antecedentes",
                                 use_container_width=True, disabled=not antecedentes):
                        st.session_state.panel_documental_activo = (
                            None if st.session_state.panel_documental_activo == "antecedentes" else "antecedentes"
                        )
                    if st.button("📄 Oficio firmado", key="btn_tec_firmado",
                                 use_container_width=True, disabled=not firmado):
                        st.session_state.panel_documental_activo = (
                            None if st.session_state.panel_documental_activo == "firmado" else "firmado"
                        )
                with c2:
                    if st.button("📎 Anexos", key="btn_tec_anexos",
                                 use_container_width=True, disabled=not anexos):
                        st.session_state.panel_documental_activo = (
                            None if st.session_state.panel_documental_activo == "anexos" else "anexos"
                        )
                    if st.button("♙ Responsable", key="btn_tec_responsable",
                                 use_container_width=True, disabled=not responsable):
                        st.session_state.panel_documental_activo = (
                            None if st.session_state.panel_documental_activo == "responsable" else "responsable"
                        )

                activo = st.session_state.get("panel_documental_activo")
                grupos = {"antecedentes": antecedentes, "anexos": anexos, "firmado": firmado}
                if activo in grupos:
                    st.markdown(f"**{activo.capitalize()} disponibles**")
                    for i, documento in enumerate(grupos[activo]):
                        st.link_button(
                            documento["nombre"], documento["url"],
                            key=f"documento_{activo}_{i}", use_container_width=True
                        )
                elif activo == "responsable":
                    st.info(f"Responsable asignado: {responsable}")
                elif not oficio_encontrado:
                    st.caption("Los accesos estarán disponibles al localizar un oficio registrado.")
                elif not (antecedentes or anexos or firmado):
                    st.caption("Este oficio aún no tiene documentos vinculados.")

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

    if st.button(
        "＋ Registrar nuevo oficio"
        if not st.session_state.mostrar_registro
        else "− Cerrar registro",
        key="abrir_registro_oficio",
        type="primary",
        use_container_width=True
    ):
        st.session_state.mostrar_registro = (
            not st.session_state.mostrar_registro
        )

    if st.session_state.mostrar_registro:
        with st.container(border=True):
            formulario_nuevo_oficio()


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