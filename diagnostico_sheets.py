from pathlib import Path
from urllib.parse import urlparse
import re
import json

from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

RUTA = Path(__file__).parent
ARCHIVO = RUTA / "credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets"
]

# Cargar credenciales
credenciales = Credentials.from_service_account_file(
    str(ARCHIVO),
    scopes=SCOPES
)

print("\n=== DIAGNÓSTICO GOOGLE SHEETS ===")
print("Cuenta:", credenciales.service_account_email)
print("Proyecto:", credenciales.project_id)

# Usar la URL copiada directamente del navegador
url = input("\nPega la URL completa de Google Sheets: ").strip()

coincidencia = re.search(r"/spreadsheets/d/([^/?#]+)", url)

if not coincidencia:
    raise ValueError("La URL no contiene un ID válido de Sheets")

spreadsheet_id = coincidencia.group(1)

print("ID detectado:", repr(spreadsheet_id))
print("Longitud del ID:", len(spreadsheet_id))

# Consultar directamente Google Sheets API
sesion = AuthorizedSession(credenciales)

endpoint = (
    f"https://sheets.googleapis.com/v4/spreadsheets/"
    f"{spreadsheet_id}"
)

respuesta = sesion.get(
    endpoint,
    params={"fields": "spreadsheetId,properties.title,sheets.properties.title"},
    timeout=30
)

print("\nCódigo HTTP:", respuesta.status_code)

try:
    datos = respuesta.json()
except ValueError:
    datos = {"respuesta": respuesta.text[:1000]}

print(json.dumps(datos, indent=2, ensure_ascii=False))

if respuesta.status_code == 200:
    print("\nCONEXIÓN EXITOSA")
elif respuesta.status_code == 403:
    print("\nACCESO DENEGADO O API SIN PERMISOS")
elif respuesta.status_code == 404:
    print("\nDOCUMENTO NO ENCONTRADO O INACCESIBLE")
else:
    print("\nREVISAR DETALLE DEL ERROR")
