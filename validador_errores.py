#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validador_errores.py — Herramientas para leer y cruzar errores de validación ARCA
con el Libro Sueldo Digital (LSD).

Flujo típico:
  1. ARCA devuelve un archivo CSV con errores de validación luego de la presentación.
  2. Este módulo parsea ese CSV y cruza los errores con el TXT del LSD.
  3. El agente puede así diagnosticar la causa raíz de cada error.

Formato del archivo de errores ARCA:
  Cuil/Dato de referencia; Descripción
  CUIL: 20318798355; La base imponible 9 informada a nivel de nomina (650.305,28) 
        difiere de la determinada (644.400,35) a partir de las liquidaciones ingresadas.
"""

import re
from collections import defaultdict

# ── Constantes compartidas ───────────────────────────────────────────────────
TIPO_START  = 0;  TIPO_END    = 2
CUIL_START  = 2;  CUIL_END    = 13
REG03_COD_START = 13; REG03_COD_END = 20
REG03_IMP_START = 29; REG03_IMP_END = 44  # importe en centavos implícitos (15 chars)
REG03_DC_POS    = 44                       # 'C'=crédito / 'D'=débito

# Mapeo base imponible → descripción legible
NOMBRE_BASE = {
    1:  "Base 1 — SIPA (aportes jubilación)",
    2:  "Base 2 — SIPA (contribuciones)",
    3:  "Base 3 — FNE",
    4:  "Base 4 — Obra Social / FSR (aportes)",
    5:  "Base 5 — INSSJP / PAMI",
    6:  "Base 6 — LRT",
    7:  "Base 7 — Régimen diferencial",
    8:  "Base 8 — Obra Social (contribuciones)",
    9:  "Base 9 — Rem. neta de detracción (Ley 27.430)",
    10: "Base 10 — Otra",
}

# Posiciones de cada base en REG04 (0-indexed, 15 chars cada una)
# Según spec AFIP LSD v2 y LibroDigitalReg04.java
BASE_POSICIONES = {
    1:  (175, 190),   # Rem. Bruta / Base 1 SIPA
    2:  (190, 205),   # Base 2 contribuciones
    3:  (205, 220),   # Base 3 FNE
    4:  (220, 235),   # Base 4 OS/FSR
    5:  (235, 250),   # Base 5 INSSJP
    6:  (250, 265),   # Base 6 LRT
    7:  (265, 280),   # Base 7
    8:  (280, 295),   # Base 8 OS contrib.
    9:  (295, 310),   # Base 9
    10: (355, 370),   # Base 10 / Rem10
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _leer_lineas(ruta: str) -> list[str]:
    for enc in ('utf-8', 'latin-1', 'cp1252'):
        try:
            with open(ruta, encoding=enc) as f:
                return [l.rstrip('\r\n') for l in f]
        except UnicodeDecodeError:
            continue
    raise ValueError(f"No se pudo decodificar: {ruta}")


def _tipo(l: str) -> str:
    return l[TIPO_START:TIPO_END] if len(l) >= TIPO_END else '??'


def _cuil(l: str) -> str:
    return l[CUIL_START:CUIL_END] if len(l) >= CUIL_END else '?'


def _formatear_cuil(cuil_raw: str) -> str:
    """Normaliza un CUIL a 11 dígitos sin guiones."""
    solo_digitos = re.sub(r'\D', '', cuil_raw)
    return solo_digitos.zfill(11)


def _centavos_a_pesos(s: str) -> float:
    """Convierte un campo numérico LSD (centavos implícitos, sin punto) a pesos."""
    s = s.strip()
    if not s or not re.match(r'^\d+$', s):
        return 0.0
    return int(s) / 100.0


def _pesos_str(val: float) -> str:
    return f"${val:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')


# ── Parseo del archivo de errores ARCA ───────────────────────────────────────

# Patrones de error conocidos
_RE_BASE_IMPONIBLE = re.compile(
    r'base imponible\s+(\d+)\s+informada\s+a\s+nivel\s+de\s+n.mina\s*'
    r'\(\s*([\d.,]+)\s*\)\s+difiere\s+de\s+la\s+determinada\s*\(\s*([\d.,]+)\s*\)',
    re.IGNORECASE
)

_RE_CUIL_HEADER = re.compile(
    r'CUIL\s*:\s*([\d\-]+)',
    re.IGNORECASE
)


def _parsear_monto_arca(s: str) -> float:
    """Convierte un monto en formato ARCA '650.305,28' a float."""
    s = s.strip().replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def tool_parsear_errores_arca(ruta_errores: str) -> dict:
    """
    Lee el archivo CSV de errores de validación que devuelve ARCA después de presentar,
    y devuelve un resumen estructurado de todos los errores encontrados.

    Detecta el patrón más común:
      "La base imponible N informada a nivel de nómina (X) difiere de la determinada (Y)"
    
    También captura otros errores que no encajan en ese patrón.
    """
    try:
        lineas = _leer_lineas(ruta_errores)
        errores: list[dict] = []
        no_reconocidos: list[str] = []

        # Saltar encabezado
        for linea in lineas:
            linea = linea.strip()
            if not linea:
                continue
            # Separar CUIL y descripción
            partes = linea.split(';', 1)
            if len(partes) < 2:
                continue
            cuil_raw, descripcion = partes[0].strip(), partes[1].strip()

            # Ignorar encabezado
            if 'cuil' in cuil_raw.lower() and 'descripci' in descripcion.lower():
                continue

            # Extraer CUIL
            m_cuil = _RE_CUIL_HEADER.search(cuil_raw)
            if not m_cuil:
                no_reconocidos.append(linea[:80])
                continue
            cuil = _formatear_cuil(m_cuil.group(1))

            # ¿Es error de base imponible?
            m_base = _RE_BASE_IMPONIBLE.search(descripcion)
            if m_base:
                num_base    = int(m_base.group(1))
                informado   = _parsear_monto_arca(m_base.group(2))
                determinado = _parsear_monto_arca(m_base.group(3))
                diferencia  = informado - determinado
                errores.append({
                    "tipo":          "base_imponible_incorrecta",
                    "cuil":          cuil,
                    "base_numero":   num_base,
                    "base_nombre":   NOMBRE_BASE.get(num_base, f"Base {num_base}"),
                    "informado":     informado,
                    "determinado":   determinado,
                    "diferencia":    round(diferencia, 2),
                    "descripcion":   descripcion[:120],
                })
            else:
                errores.append({
                    "tipo":        "otro_error",
                    "cuil":        cuil,
                    "descripcion": descripcion[:120],
                })

        # Resumen por tipo de base
        por_base: dict[int, list] = defaultdict(list)
        por_cuil: dict[str, list] = defaultdict(list)
        for e in errores:
            por_cuil[e['cuil']].append(e)
            if e['tipo'] == 'base_imponible_incorrecta':
                por_base[e['base_numero']].append(e)

        resumen_bases = {
            num: {
                "cuils_afectados": len(lista),
                "diferencia_total": round(sum(e['diferencia'] for e in lista), 2),
                "diferencia_promedio": round(
                    sum(e['diferencia'] for e in lista) / len(lista), 2
                ) if lista else 0,
                "nombre": NOMBRE_BASE.get(num, f"Base {num}"),
            }
            for num, lista in por_base.items()
        }

        return {
            "ok":                True,
            "total_errores":     len(errores),
            "cuils_afectados":   len(por_cuil),
            "errores_por_base":  resumen_bases,
            "no_reconocidos":    no_reconocidos,
            "detalle":           errores,
        }

    except FileNotFoundError:
        return {"ok": False, "error": f"Archivo de errores no encontrado: {ruta_errores}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Cruce errores ARCA con LSD ───────────────────────────────────────────────

def tool_cruzar_errores_con_lsd(ruta_errores: str, ruta_lsd: str) -> dict:
    """
    Cruza los errores de validación ARCA con el LSD para diagnosticar la causa raíz.

    Para cada CUIL con error de base imponible:
      1. Lee los conceptos REG03 del CUIL en el LSD
      2. Calcula la suma de remunerativos (créditos) y descuentos (débitos)
      3. Lee la base informada en REG04
      4. Muestra la diferencia entre lo que el LSD dice y lo que ARCA esperaba
      5. Identifica si la diferencia se explica por la suma de conceptos
    """
    try:
        # Parsear errores
        resultado_errores = tool_parsear_errores_arca(ruta_errores)
        if not resultado_errores['ok']:
            return resultado_errores

        # Leer LSD
        lineas_lsd = _leer_lineas(ruta_lsd)

        # Indexar conceptos REG03 por CUIL
        conceptos_por_cuil: dict[str, list[dict]] = defaultdict(list)
        for l in lineas_lsd:
            if not l.strip() or _tipo(l) != '03':
                continue
            cuil = _cuil(l)
            cod  = l[REG03_COD_START:REG03_COD_END].strip() if len(l) >= REG03_COD_END else ''
            imp_raw = l[REG03_IMP_START:REG03_IMP_END].strip() if len(l) > REG03_IMP_END else '0'
            dc   = l[REG03_DC_POS] if len(l) > REG03_DC_POS else 'C'
            importe = _centavos_a_pesos(imp_raw)
            if dc == 'D':
                importe = -importe
            conceptos_por_cuil[cuil].append({
                "codigo_arca": cod,
                "importe": importe,
                "dc": dc,
            })

        # Indexar REG04 por CUIL
        reg04_por_cuil: dict[str, str] = {}
        for l in lineas_lsd:
            if not l.strip() or _tipo(l) != '04':
                continue
            reg04_por_cuil[_cuil(l)] = l

        # Cruzar
        diagnosticos: list[dict] = []

        # Agrupar errores por CUIL para no repetir el diagnóstico
        errores_por_cuil: dict[str, list[dict]] = defaultdict(list)
        for e in resultado_errores['detalle']:
            if e['tipo'] == 'base_imponible_incorrecta':
                errores_por_cuil[e['cuil']].append(e)

        for cuil, errores_cuil in errores_por_cuil.items():
            conceptos = conceptos_por_cuil.get(cuil, [])
            reg04     = reg04_por_cuil.get(cuil)

            # Calcular suma de conceptos remunerativos desde REG03
            total_remunerativos = sum(c['importe'] for c in conceptos if c['importe'] > 0)
            total_descuentos    = sum(c['importe'] for c in conceptos if c['importe'] < 0)

            # Leer bases desde REG04
            bases_reg04: dict[int, float] = {}
            if reg04:
                for num_base, (inicio, fin) in BASE_POSICIONES.items():
                    if len(reg04) >= fin:
                        val_raw = reg04[inicio:fin].strip()
                        bases_reg04[num_base] = _centavos_a_pesos(val_raw)

            # Armar diagnóstico por error
            errores_diagnosticados = []
            for err in errores_cuil:
                num_base   = err['base_numero']
                informado  = err['informado']
                esperado   = err['determinado']
                diferencia = err['diferencia']

                base_en_lsd = bases_reg04.get(num_base, None)

                # ¿La base en el LSD coincide con lo que ARCA dice que se informó?
                coincide_con_lsd = (
                    base_en_lsd is not None
                    and abs(base_en_lsd - informado) < 1.0
                )

                # ¿La diferencia se explica por la suma de conceptos?
                diferencia_vs_conceptos = None
                if num_base in (1, 2, 3):
                    diferencia_vs_conceptos = round(total_remunerativos - informado, 2)

                errores_diagnosticados.append({
                    "base_numero":              num_base,
                    "base_nombre":              NOMBRE_BASE.get(num_base, f"Base {num_base}"),
                    "informado_segun_arca":     informado,
                    "determinado_por_arca":     esperado,
                    "diferencia_arca":          diferencia,
                    "base_en_lsd":              base_en_lsd,
                    "coincide_lsd_con_arca":    coincide_con_lsd,
                    "diferencia_vs_conceptos":  diferencia_vs_conceptos,
                    "interpretacion":           _interpretar_diferencia(
                        num_base, diferencia, base_en_lsd, informado, total_remunerativos
                    ),
                })

            diagnosticos.append({
                "cuil":                  cuil,
                "errores_arca":          len(errores_cuil),
                "conceptos_en_lsd":      len(conceptos),
                "total_remunerativos":   total_remunerativos,
                "total_descuentos":      total_descuentos,
                "tiene_reg04":           reg04 is not None,
                "bases_en_reg04":        bases_reg04,
                "diagnostico_por_error": errores_diagnosticados,
            })

        # Resumen global
        cuils_sin_conceptos = [d['cuil'] for d in diagnosticos if d['conceptos_en_lsd'] == 0]
        cuils_sin_reg04     = [d['cuil'] for d in diagnosticos if not d['tiene_reg04']]

        return {
            "ok":                   True,
            "total_errores_arca":   resultado_errores['total_errores'],
            "cuils_con_error":      len(errores_por_cuil),
            "cuils_sin_conceptos_en_lsd": cuils_sin_conceptos,
            "cuils_sin_reg04_en_lsd":     cuils_sin_reg04,
            "diagnostico_detallado":      diagnosticos,
            "resumen_para_consultor": _resumen_legible(diagnosticos),
        }

    except FileNotFoundError as e:
        return {"ok": False, "error": f"Archivo no encontrado: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _interpretar_diferencia(
    num_base: int,
    diferencia: float,
    base_en_lsd: float | None,
    informado: float,
    total_remunerativos: float
) -> str:
    """Genera una interpretación en lenguaje simple de la diferencia."""
    abs_dif = abs(diferencia)

    if base_en_lsd is None:
        return (
            "No se encontró el REG04 del empleado en el LSD. "
            "El archivo podría estar incompleto."
        )

    if abs_dif < 1.0:
        return "La diferencia es menor a $1. Puede ser un redondeo. Verificar."

    if num_base == 9:
        if diferencia > 0:
            return (
                f"La base informada (${informado:,.2f}) es MAYOR que la determinada por ARCA. "
                f"Diferencia: ${abs_dif:,.2f}. "
                "Probable causa: la detracción (Ley 27.430) no se aplicó correctamente "
                "o hay conceptos sumándose que no deberían."
            )
        else:
            return (
                f"La base informada (${informado:,.2f}) es MENOR que la determinada por ARCA. "
                f"Diferencia: ${abs_dif:,.2f}. "
                "Probable causa: se aplicó una detracción mayor a la correcta, "
                "o falta acumular conceptos remunerativos."
            )

    if num_base in (1, 2, 3):
        dif_conceptos = abs(total_remunerativos - informado)
        if dif_conceptos < 1.0:
            return (
                "La suma de conceptos REG03 coincide con lo informado. "
                "La diferencia con ARCA puede ser por un tope MOPRE distinto o un concepto mal configurado."
            )
        return (
            f"La suma de conceptos remunerativos en el LSD es ${total_remunerativos:,.2f}, "
            f"pero se informó ${informado:,.2f}. Diferencia interna: ${dif_conceptos:,.2f}. "
            "Revisar si hay conceptos faltantes o duplicados en REG03."
        )

    return (
        f"Diferencia de ${abs_dif:,.2f} en {NOMBRE_BASE.get(num_base, f'Base {num_base}')}. "
        "Verificar la configuración del concepto en el sistema y las marcas de obra social/FSR."
    )


def _resumen_legible(diagnosticos: list[dict]) -> str:
    """Genera un resumen en lenguaje simple para el consultor."""
    if not diagnosticos:
        return "No se encontraron cruces entre los errores ARCA y el LSD."

    total_cuils  = len(diagnosticos)
    total_errores = sum(d['errores_arca'] for d in diagnosticos)

    lineas = [
        f"Se encontraron {total_errores} errores de base imponible en {total_cuils} empleados.",
        "",
    ]

    for d in diagnosticos[:5]:  # Mostrar primeros 5 en el resumen
        cuil = d['cuil']
        lineas.append(f"  CUIL {cuil}:")
        for err in d['diagnostico_por_error']:
            lineas.append(
                f"    • {err['base_nombre']}: "
                f"diferencia de ${abs(err['diferencia_arca']):,.2f} — "
                f"{err['interpretacion'][:80]}..."
            )
        lineas.append("")

    if total_cuils > 5:
        lineas.append(f"  ... y {total_cuils - 5} empleados más. Ver detalle completo.")

    return "\n".join(lineas)


# ── Definición de herramientas para la API de Claude ─────────────────────────

TOOLS_ERRORES = [
    {
        "name": "parsear_errores_arca",
        "description": (
            "Lee el archivo CSV de errores de validación que devuelve ARCA después de presentar "
            "el Libro Sueldo Digital. Identifica y estructura todos los errores: "
            "errores de base imponible (la más frecuente), y otros tipos de error. "
            "Devuelve un resumen por tipo de base y los CUILs afectados. "
            "Llamar siempre que el usuario suba un archivo de errores ARCA."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ruta_errores": {
                    "type": "string",
                    "description": "Ruta al archivo TXT/CSV de errores de validación ARCA"
                }
            },
            "required": ["ruta_errores"]
        }
    },
    {
        "name": "cruzar_errores_con_lsd",
        "description": (
            "Cruza los errores de validación ARCA con el LSD para encontrar la causa raíz "
            "de cada error. Para cada empleado con error, compara lo que dice el LSD "
            "(conceptos REG03 y bases REG04) con lo que ARCA esperaba. "
            "Devuelve un diagnóstico en lenguaje simple para el consultor. "
            "Llamar DESPUÉS de parsear_errores_arca, solo si hay un LSD disponible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ruta_errores": {
                    "type": "string",
                    "description": "Ruta al archivo TXT/CSV de errores de validación ARCA"
                },
                "ruta_lsd": {
                    "type": "string",
                    "description": "Ruta al archivo TXT del LSD correspondiente al mismo período"
                }
            },
            "required": ["ruta_errores", "ruta_lsd"]
        }
    },
]

TOOL_FUNCTIONS_ERRORES = {
    "parsear_errores_arca":    tool_parsear_errores_arca,
    "cruzar_errores_con_lsd":  tool_cruzar_errores_con_lsd,
}
