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

from io import BytesIO
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


# PASO 2: permisos. El responsable se configura por cuenta en secrets.toml.
# No se conceden permisos de edición por coincidencias parciales de nombres.
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


@st.cache_data(ttl=60, show_spinner=False)
def obtener_oficios():
    try:
        hoja = conectar_google_sheets()
        registros = hoja.get_all_records()
        return pd.DataFrame(registros)
    except Exception:
        return pd.DataFrame()


def guardar_oficio(datos):
    exigir_sesion()
    hoja = conectar_google_sheets()
    hoja.append_row(
        datos,
        value_input_option="USER_ENTERED"
    )
    obtener_oficios.clear()


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
    # Un auxiliar no puede transferir un oficio a otra persona.
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
    obtener_oficios.clear()


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
    obtener_oficios.clear()


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
    """Reporte imprimible: fechas, observaciones y firmas según los responsables[cite: 7]."""
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
# HOME
# =========================================================

if st.session_state.menu_activo == "home":
    aplicar_fondo()

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

    # =========================================================
    # ESTILO GRAFITO Y AZUL ACERO — MÓDULO OFICIOS
    # =========================================================
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
            [data-testid="stAppViewContainer"] {{
                background-image: url("data:image/png;base64,{fondo_oficios}") !important;
                background-size: cover !important;
                background-position: center center !important;
                background-repeat: no-repeat !important;
                background-attachment: fixed !important;
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
    st.markdown("<br>", unsafe_allow_html=True)
    st.divider()
    st.markdown(
        "<p style='text-align:center;color:#667784;font-size:11px;font-weight:600;letter-spacing:2px;'>ADMINISTRACIÓN DOCUMENTAL</p>",
        unsafe_allow_html=True
    )
    st.markdown("<br>", unsafe_allow_html=True)

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