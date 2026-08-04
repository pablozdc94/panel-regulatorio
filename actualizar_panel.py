#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
actualizar_panel.py — sincronización diaria del panel de alerta regulatoria.

QUÉ HACE
--------
1. CONGRESO (automático y confiable). Consulta el servicio público de
   tramitación del Senado para cada boletín en seguimiento y actualiza
   estado, trámite, comisión, urgencia y fecha del último movimiento
   directamente en el archivo HTML del panel.

2. REGULADORES (vigilancia, no automatización ciega). CMF, UAF y Banco
   Central no publican un servicio de datos estructurado equivalente.
   El script descarga sus páginas de normativa, calcula una huella del
   contenido y avisa cuando cambia respecto de la corrida anterior.
   No inventa alertas: deja el cambio en la bitácora para que una
   persona lo lea, lo clasifique y escriba el extracto a mano.

   Esta división es deliberada. Un resumen normativo generado sin
   revisión humana es exactamente el tipo de error que un panel de
   cumplimiento no puede permitirse.

USO
---
    python3 actualizar_panel.py --panel panel-alerta-regulatoria-cmf.html

PROGRAMACIÓN DIARIA
-------------------
Linux/macOS (cron, todos los días a las 07:15):
    15 7 * * * cd /ruta/al/panel && /usr/bin/python3 actualizar_panel.py \
        --panel panel-alerta-regulatoria-cmf.html >> sync.log 2>&1

Windows (Programador de tareas):
    Acción: python.exe
    Argumentos: actualizar_panel.py --panel panel-alerta-regulatoria-cmf.html
    Iniciar en: C:\\ruta\\al\\panel
    Desencadenador: diariamente 07:15

DEPENDENCIAS
------------
Ninguna. Solo Python 3.9 o superior, que ya viene instalado en macOS y Linux.
En Windows se descarga desde python.org marcando "Add Python to PATH".
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=-4))  # Chile continental
UA = {"User-Agent": "PanelAlertaRegulatoria/1.0 (uso interno de cumplimiento)"}
TIMEOUT = 30
CONTEXTO = ssl.create_default_context()


def descargar(url):
    """Descarga una URL y devuelve su texto. Lanza excepción si falla."""
    pedido = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(pedido, timeout=TIMEOUT, context=CONTEXTO) as r:
        bruto = r.read()
    for codec in ("utf-8", "latin-1"):
        try:
            return bruto.decode(codec)
        except UnicodeDecodeError:
            continue
    return bruto.decode("utf-8", errors="replace")

# Servicio público de tramitación del Senado. Devuelve XML por boletín.
WS_SENADO = "https://www.senado.cl/wspublico/tramitacion.php?boletin={num}"

# Páginas de normativa bajo vigilancia. Ajusta o agrega según el perímetro.
FUENTES_VIGILADAS = {
    "UAF · circulares vigentes":  "https://www.uaf.cl/es-cl/normativa/circulares-uaf",
    "UAF · normativa en consulta": "https://www.uaf.cl/legislacion/normativa_tramite.aspx",
    "CMF · normativa en consulta": "https://www.cmfchile.cl/institucional/legislacion_normativa/normativa_tramite.php",
    "CMF · emitida último mes":    "https://www.cmfchile.cl/institucional/legislacion_normativa/normativa2.php?ultima=mes",
    "BCCh · normativa":            "https://www.bcentral.cl/web/banco-central/areas/normativa",
    "BCCh · CNCI (cambios internacionales)": "https://www.bcentral.cl/areas/compendio-de-normas-de-cambios-internacionales",
}

# Palabras que marcan un cambio como potencialmente relevante para la operación.
PALABRAS_CLAVE = [
    "remesa", "transferencia", "cambio internacional", "lavado de activos",
    "financiamiento del terrorismo", "beneficiario final", "debida diligencia",
    "operaciones sospechosas", "fintec", "prestador de servicios financieros",
    "medios de pago", "transfronteriz", "sujeto obligado", "congelamiento",
]

ESTADO = Path("estado_sync.json")   # huellas de la corrida anterior
BITACORA = Path("bitacora_sync.md") # cambios para revisión humana


# --------------------------------------------------------------------------
# Congreso
# --------------------------------------------------------------------------
def consultar_boletin(boletin):
    """Devuelve los campos de tramitación de un boletín, o None si falla."""
    num = boletin.split("-")[0]
    try:
        raiz = ET.fromstring(descargar(WS_SENADO.format(num=num)).encode("utf-8"))
    except Exception as e:
        print(f"  ! {boletin}: {e}")
        return None

    def texto(nodo, etiqueta):
        el = nodo.find(f".//{etiqueta}")
        return (el.text or "").strip() if el is not None and el.text else ""

    proyecto = raiz.find(".//proyecto")
    if proyecto is None:
        return None

    tramites = proyecto.findall(".//tramite")
    ultimo = tramites[-1] if tramites else None
    urgencias = proyecto.findall(".//urgencia")
    urgencia_actual = ""
    if urgencias:
        urgencia_actual = (texto(urgencias[-1], "TIPO") or "").strip().capitalize()

    fecha = ""
    if ultimo is not None:
        crudo = texto(ultimo, "FECHA")
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", crudo)
        if m:
            fecha = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    return {
        "estado": texto(proyecto, "ESTADO") or "En tramitación",
        "camara": texto(proyecto, "CAMARA_ORIGEN") or "Por confirmar",
        "tramite": (texto(ultimo, "DESCRIPCIONTRAMITE") if ultimo is not None else "") or "Por confirmar",
        "comision": (texto(ultimo, "SESION") if ultimo is not None else "") or "Por confirmar",
        "urgencia": urgencia_actual or "Sin urgencia",
        "fecha": fecha,
    }


def actualizar_proyectos(html):
    """Reescribe los campos automáticos de cada boletín dentro del HTML."""
    boletines = re.findall(r"boletin:'([\d\-]+)'", html)
    print(f"Congreso: {len(boletines)} boletines en seguimiento")
    novedades = 0

    for boletin in boletines:
        datos = consultar_boletin(boletin)
        if not datos:
            continue

        # Aísla el objeto de este boletín para no tocar a los demás.
        patron = re.compile(
            r"(\{\s*\n\s*boletin:'" + re.escape(boletin) + r"'.*?\n  \})",
            re.S,
        )
        m = patron.search(html)
        if not m:
            continue
        bloque = original = m.group(1)

        for campo in ("camara", "estado", "tramite", "comision", "urgencia", "fecha"):
            valor = datos[campo].replace("'", "\\'")
            if not valor:
                continue
            bloque = re.sub(
                rf"{campo}:'[^']*'", f"{campo}:'{valor}'", bloque, count=1
            )

        if bloque != original:
            html = html.replace(original, bloque, 1)
            novedades += 1
            print(f"  · {boletin}: actualizado")

    return html, novedades


# --------------------------------------------------------------------------
# Reguladores
# --------------------------------------------------------------------------
def vigilar_fuentes():
    """Compara huellas de las páginas de normativa y registra los cambios."""
    previo = json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {}
    actual, ok, falla, cambios = {}, [], [], []

    for nombre, url in FUENTES_VIGILADAS.items():
        try:
            crudo = descargar(url)
            texto = re.sub(r"<script.*?</script>|<style.*?</style>", " ", crudo, flags=re.S | re.I)
            texto = re.sub(r"<[^>]+>", " ", texto)
            texto = re.sub(r"\s+", " ", texto).strip().lower()
            actual[nombre] = hashlib.sha256(texto.encode("utf-8")).hexdigest()
            ok.append(nombre)

            if nombre in previo and previo[nombre] != actual[nombre]:
                encontradas = sorted({p for p in PALABRAS_CLAVE if p in texto})
                cambios.append((nombre, url, encontradas))
                print(f"  * {nombre}: contenido modificado")
        except Exception as e:
            falla.append(nombre)
            actual[nombre] = previo.get(nombre, "")
            print(f"  ! {nombre}: {e}")

    ESTADO.write_text(json.dumps(actual, ensure_ascii=False, indent=2), encoding="utf-8")

    if cambios:
        hoy = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        lineas = [f"\n## {hoy}\n"]
        for nombre, url, palabras in cambios:
            lineas.append(f"- **{nombre}** cambió. {url}")
            if palabras:
                lineas.append(f"  - Términos del perímetro detectados: {', '.join(palabras)}")
            lineas.append("  - [ ] Revisado y clasificado")
            lineas.append("  - [ ] Alerta creada o descartada en el panel")
        with BITACORA.open("a", encoding="utf-8") as f:
            f.write("\n".join(lineas) + "\n")

    return ok, falla, len(cambios)


# --------------------------------------------------------------------------
# Sello
# --------------------------------------------------------------------------
def sellar(html, ok, falla, novedades):
    ahora = dt.datetime.now(TZ).replace(microsecond=0).isoformat()
    nuevo = (
        "const SINCRONIZACION = {\n"
        f"  ultima: '{ahora}',\n"
        f"  fuentesOk: {json.dumps(ok, ensure_ascii=False)},\n"
        f"  fuentesFalla: {json.dumps(falla, ensure_ascii=False)},\n"
        f"  novedades: {novedades}\n"
        "};"
    )
    return re.sub(r"const SINCRONIZACION = \{.*?\};", nuevo, html, count=1, flags=re.S)


def main():
    ap = argparse.ArgumentParser(description="Sincroniza el panel de alerta regulatoria.")
    ap.add_argument("--panel", required=True, help="Ruta del archivo HTML del panel")
    ap.add_argument("--solo-congreso", action="store_true", help="Omite la vigilancia de reguladores")
    ap.add_argument("--inspeccionar", metavar="BOLETIN",
                    help="Imprime el XML crudo de un boletín para verificar los nombres de los campos "
                         "antes de la primera corrida real. Ej: --inspeccionar 18441-03")
    args = ap.parse_args()

    if args.inspeccionar:
        num = args.inspeccionar.split("-")[0]
        print(descargar(WS_SENADO.format(num=num))[:6000])
        return

    ruta = Path(args.panel)
    if not ruta.exists():
        sys.exit(f"No se encontró el panel en {ruta}")

    html = ruta.read_text(encoding="utf-8")

    # Respaldo antes de escribir: si algo sale mal, se vuelve atrás.
    respaldo = ruta.with_suffix(f".{dt.date.today():%Y%m%d}.bak.html")
    respaldo.write_text(html, encoding="utf-8")

    html, novedades = actualizar_proyectos(html)

    ok, falla = [], []
    if not args.solo_congreso:
        print("Reguladores: vigilando fuentes")
        ok, falla, cambios = vigilar_fuentes()
        novedades += cambios

    ok = ["Congreso Nacional"] + ok
    html = sellar(html, ok, falla, novedades)
    ruta.write_text(html, encoding="utf-8")

    print(f"\nListo. {novedades} novedades. Respaldo en {respaldo.name}")
    if BITACORA.exists() and not args.solo_congreso:
        print(f"Revisa {BITACORA.name}: los cambios de los reguladores requieren lectura humana.")


if __name__ == "__main__":
    main()
