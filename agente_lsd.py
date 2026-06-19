#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agente_lsd.py — Agente Gemini de validación LSD (Libro Sueldo Digital) para ARCA/AFIP
=======================================================================================
[Misma funcionalidad que la versión Anthropic, adaptado a Google Gemini 3.5 Flash]

USO:
    python agente_lsd.py <archivo.txt>
    python agente_lsd.py ErroresValidacion_30663343501_20260507.txt

REQUISITOS:
    pip install google-genai
    Variable de entorno: GEMINI_API_KEY=AIza...

    Opcional (base de conocimiento PDF):
    Correr primero: python knowledge_loader.py
"""

import sys
import os
import json
import re
from collections import defaultdict

# ── Importar herramientas de cruce con errores ARCA (opcional) ────────────────
try:
    from validador_errores import TOOLS_ERRORES, TOOL_FUNCTIONS_ERRORES
except ImportError:
    TOOLS_ERRORES = []
    TOOL_FUNCTIONS_ERRORES = {}

# ── Importar refs de PDFs de normativa (opcional) ────────────────────────────
try:
    from knowledge_loader import cargar_refs
except ImportError:
    def cargar_refs(): return []


# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------

MODELO = "gemini-2.5-flash"

# ---------------------------------------------------------------------------
# Constantes de formato LSD (0-indexed, spec AFIP LSD v2.x)
# ---------------------------------------------------------------------------

TIPO_START = 0;  TIPO_END   = 2
CUIL_START = 2;  CUIL_END   = 13
REG03_COD_START = 13; REG03_COD_END = 20
REG03_IMP_START = 29; REG03_IMP_END = 44
REG04_BASE4_START  = 220; REG04_BASE4_END    = 235
REG04_BASE10_START = 340; REG04_BASE10_END   = 355

# Nuevas constantes — spec LSD v2 completa (posiciones 0-indexed)
REG01_PERIODO_START       = 15;  REG01_PERIODO_END       = 21
REG01_CANT_REG04_START    = 29;  REG01_CANT_REG04_END    = 35
REG04_REM_BRUTA_START     = 160; REG04_REM_BRUTA_END     = 175
REG04_BASE1_START         = 175; REG04_BASE1_END         = 190
REG04_HORAS_EXTRAS_START  = 340; REG04_HORAS_EXTRAS_END  = 355

# Longitudes exactas por tipo (ancho fijo)
LONGITUDES_REQUERIDAS = {
    '01': 35,
    '02': 115,
    '03': 51,   # sin el sufijo ".e-s" de e-SUELDOS (aceptable hasta 55)
    '04': 370,  # mínimo; el CBU y forma de pago se agregan al final
    '05': 65,
}

# ---------------------------------------------------------------------------
# Lectura del archivo
# ---------------------------------------------------------------------------

def _leer_lineas(ruta: str) -> list[str]:
    for enc in ('utf-8', 'latin-1', 'cp1252'):
        try:
            with open(ruta, encoding=enc) as f:
                return [l.rstrip('\r\n') for l in f]
        except UnicodeDecodeError:
            continue
    raise ValueError(f"No se pudo decodificar {ruta}")

def _tipo(linea: str) -> str:
    return linea[TIPO_START:TIPO_END] if len(linea) >= TIPO_END else '??'

def _cuil(linea: str) -> str:
    return linea[CUIL_START:CUIL_END] if len(linea) >= CUIL_END else '?'

# ---------------------------------------------------------------------------
# HERRAMIENTAS DE VALIDACIÓN
# ---------------------------------------------------------------------------

def tool_info_archivo(ruta: str) -> dict:
    try:
        lineas = _leer_lineas(ruta)
        conteo: dict[str, int] = defaultdict(int)
        for l in lineas:
            if l.strip():
                conteo[_tipo(l)] += 1
        return {
            "ok": True,
            "ruta": os.path.abspath(ruta),
            "size_bytes": os.path.getsize(ruta),
            "total_lineas": len(lineas),
            "lineas_vacias": sum(1 for l in lineas if not l.strip()),
            "registros_por_tipo": dict(conteo),
        }
    except FileNotFoundError:
        return {"ok": False, "error": f"Archivo no encontrado: {ruta}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_estructura(ruta: str) -> dict:
    try:
        lineas = _leer_lineas(ruta)
        errores = []
        advertencias = []
        tipos = [_tipo(l) for l in lineas if l.strip()]
        conteo: dict[str, int] = defaultdict(int)
        for t in tipos:
            conteo[t] += 1

        if conteo.get('01', 0) == 0:
            errores.append("FALTA REG01: el archivo no tiene registro de cabecera (tipo 01).")
        elif conteo['01'] > 1:
            errores.append(f"REG01 DUPLICADO: hay {conteo['01']} registros tipo 01, debe haber exactamente 1.")
        else:
            primera = next((l for l in lineas if l.strip()), '')
            if _tipo(primera) != '01':
                errores.append("REG01 no es la primera línea del archivo.")

        if conteo.get('02', 0) == 0:
            errores.append("FALTA REG02: no hay registros de empleados (tipo 02).")

        if conteo.get('05', 0) == 0:
            advertencias.append("FALTA REG05: el archivo no tiene registro de cierre (tipo 05).")
        elif conteo['05'] > 1:
            errores.append(f"REG05 DUPLICADO: hay {conteo['05']} registros tipo 05, debe haber exactamente 1.")
        else:
            ultima = next((l for l in reversed(lineas) if l.strip()), '')
            if _tipo(ultima) != '05':
                advertencias.append("REG05 no es la última línea del archivo.")

        cuils_02: set[str] = set()
        cuils_03: set[str] = set()
        cuils_04: set[str] = set()
        for l in lineas:
            if not l.strip():
                continue
            t = _tipo(l)
            c = _cuil(l)
            if t == '02': cuils_02.add(c)
            elif t == '03': cuils_03.add(c)
            elif t == '04': cuils_04.add(c)

        huerfanos_03 = cuils_03 - cuils_02
        huerfanos_04 = cuils_04 - cuils_02
        if huerfanos_03:
            errores.append(f"REG03 HUÉRFANOS: {len(huerfanos_03)} CUILs tienen conceptos sin cabecera REG02. Ejemplos: {sorted(huerfanos_03)[:5]}")
        if huerfanos_04:
            errores.append(f"REG04 HUÉRFANOS: {len(huerfanos_04)} CUILs tienen bases sin cabecera REG02. Ejemplos: {sorted(huerfanos_04)[:5]}")

        extraños = set(conteo) - {'01', '02', '03', '04', '05'}
        if extraños:
            advertencias.append(f"Tipos de registro desconocidos: {sorted(extraños)}")

        return {
            "ok": len(errores) == 0,
            "errores": errores,
            "advertencias": advertencias,
            "resumen": {
                "reg01": conteo.get('01', 0), "reg02": conteo.get('02', 0),
                "reg03": conteo.get('03', 0), "reg04": conteo.get('04', 0),
                "reg05": conteo.get('05', 0),
                "cuils_unicos_reg02": len(cuils_02),
                "cuils_unicos_reg03": len(cuils_03),
                "cuils_unicos_reg04": len(cuils_04),
            }
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_duplicados_reg04(ruta: str) -> dict:
    try:
        lineas = _leer_lineas(ruta)
        conteo_por_cuil: dict[str, int] = defaultdict(int)
        lineas_por_cuil: dict[str, list[int]] = defaultdict(list)
        for i, l in enumerate(lineas, 1):
            if l.strip() and _tipo(l) == '04':
                c = _cuil(l)
                conteo_por_cuil[c] += 1
                lineas_por_cuil[c].append(i)
        duplicados = {
            c: {"cantidad_reg04": cnt, "en_lineas": lineas_por_cuil[c]}
            for c, cnt in conteo_por_cuil.items() if cnt > 1
        }
        return {
            "ok": len(duplicados) == 0,
            "total_reg04": sum(conteo_por_cuil.values()),
            "cuils_unicos_reg04": len(conteo_por_cuil),
            "cuils_con_duplicado": len(duplicados),
            "detalle": duplicados,
            "diagnostico": (
                "BUG A DETECTADO: Hay CUILs con más de un REG04. ARCA suma todos los REG04 → bases duplicadas (ratio 2,0x). Solución: consolidar REG04 por CUIL."
                if duplicados else "Sin duplicados REG04. OK."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_comas_numericos(ruta: str) -> dict:
    try:
        lineas = _leer_lineas(ruta)
        afectados = []
        for i, l in enumerate(lineas, 1):
            if not l.strip() or _tipo(l) not in ('03', '04'):
                continue
            campos_num = l[13:] if len(l) > 13 else ''
            if ',' in campos_num:
                posiciones = [j + 13 for j, c in enumerate(campos_num) if c == ',']
                afectados.append({
                    "linea": i, "tipo_reg": _tipo(l), "cuil": _cuil(l),
                    "posiciones_con_coma": posiciones[:10],
                    "extracto": l[max(0, posiciones[0]-5):posiciones[0]+10] if posiciones else ''
                })
        return {
            "ok": len(afectados) == 0,
            "registros_con_coma": len(afectados),
            "detalle": afectados[:50],
            "diagnostico": (
                "BUG B DETECTADO (#30999): Hay campos numéricos con coma. Guardados en formato argentino sin normalizar. ARCA rechaza el registro."
                if afectados else "Sin comas en campos numéricos. OK."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_bases_reg04(ruta: str) -> dict:
    try:
        lineas = _leer_lineas(ruta)
        afectados = []
        correctos = []
        registro_corto = 0
        for i, l in enumerate(lineas, 1):
            if not l.strip() or _tipo(l) != '04':
                continue
            if len(l) <= REG04_BASE10_END:
                registro_corto += 1
                continue
            b4_raw  = l[REG04_BASE4_START:REG04_BASE4_END].strip()
            b10_raw = l[REG04_BASE10_START:REG04_BASE10_END].strip()
            def es_num(s): return bool(s) and re.match(r'^-?\d+(\.\d+)?$', s)
            if es_num(b4_raw) and es_num(b10_raw):
                b4 = float(b4_raw); b10 = float(b10_raw)
                if b4 == 0 and b10 != 0:
                    afectados.append({"linea": i, "cuil": _cuil(l), "base4": b4_raw, "base10": b10_raw, "problema": "Base4=0 / Base10≠0 → probable concepto 560k en lugar de 570k"})
                elif b4 != 0:
                    correctos.append(_cuil(l))
        return {
            "ok": len(afectados) == 0,
            "cuils_con_base4_cero_base10_con_valor": len(afectados),
            "cuils_con_base4_correcto_no_cero": len(correctos),
            "registros_demasiado_cortos": registro_corto,
            "detalle": afectados[:30],
            "diagnostico": (
                "BUG C DETECTADO (Guía N°45): REG04 con Base4=0 y Base10 con valor. Docentes No-SIPA deben usar concepto 570.000."
                if afectados else "Sin patrón Base4=0/Base10≠0. OK."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_cbu(ruta: str) -> dict:
    try:
        lineas = _leer_lineas(ruta)
        problemas = []
        sin_cbu_candidato = 0
        for i, l in enumerate(lineas, 1):
            if not l.strip() or _tipo(l) != '04':
                continue
            cola = l[-50:] if len(l) > 50 else l[13:]
            match = re.search(r'\d{20,24}', cola)
            if match:
                bloque = match.group(0)
                if len(bloque) != 22:
                    problemas.append({"linea": i, "cuil": _cuil(l), "cbu_candidato": bloque, "longitud": len(bloque), "problema": f"CBU tiene {len(bloque)} dígitos, debe tener exactamente 22"})
            else:
                if re.search(r'[A-Za-z]', cola):
                    problemas.append({"linea": i, "cuil": _cuil(l), "cbu_candidato": None, "problema": "Cola del registro contiene letras donde se espera CBU numérico"})
                else:
                    sin_cbu_candidato += 1
        return {
            "ok": len(problemas) == 0,
            "total_reg04": sum(1 for l in lineas if l.strip() and _tipo(l) == '04'),
            "reg04_con_problema_cbu": len(problemas),
            "reg04_sin_cbu_candidato": sin_cbu_candidato,
            "detalle": problemas[:30],
            "diagnostico": ("PROBLEMA CBU: Hay REG04 con CBU de longitud incorrecta. Debe ser exactamente 22 dígitos." if problemas else "Sin problemas de CBU. OK.")
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_analizar_conceptos_reg03(ruta: str) -> dict:
    try:
        lineas = _leer_lineas(ruta)
        conteo: dict[str, int] = defaultdict(int)
        cuils_por_cod: dict[str, set] = defaultdict(set)
        for l in lineas:
            if not l.strip() or _tipo(l) != '03':
                continue
            if len(l) >= REG03_COD_END:
                cod = l[REG03_COD_START:REG03_COD_END].strip()
                cuils_por_cod[cod].add(_cuil(l))
                conteo[cod] += 1
        OBSOLETOS = {'5600000': 'Rango libre No Remunerativos Especiales → REEMPLAZAR por 570.000'}
        GUIA45 = {
            '5700000': '570.000 — Mensual Remuneración No Contributiva', '5700001': '570.001 — SAC No Contributivo',
            '5700002': '570.002 — SAC Proporcional No Contributivo', '5700003': '570.003 — Vacaciones No Contributivo',
            '8100015': '810.015 — Descuento sistema previsional no nacional', '8100016': '810.016 — Descuento obra social provincial',
        }
        alertas = [{"concepto_arca": cod, "descripcion": desc, "ocurrencias": conteo[cod], "cuils_afectados": len(cuils_por_cod[cod])} for cod, desc in OBSOLETOS.items() if cod in conteo]
        top10 = sorted(conteo.items(), key=lambda x: -x[1])[:10]
        return {
            "ok": len(alertas) == 0,
            "total_lineas_reg03": sum(conteo.values()),
            "conceptos_distintos": len(conteo),
            "top10_conceptos": [{"codigo_arca": cod, "ocurrencias": cnt, "cuils_distintos": len(cuils_por_cod[cod])} for cod, cnt in top10],
            "alertas_guia45": alertas,
            "conceptos_guia45_presentes": [cod for cod in GUIA45 if cod in conteo],
            "diagnostico": (f"ALERTA GUÍA 45: Concepto 560.000 en {alertas[0]['cuils_afectados']} CUILs. Usar 570.000 según Guía N°45." if alertas else "Sin conceptos obsoletos Guía 45. OK.")
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# NUEVAS HERRAMIENTAS DE VALIDACIÓN — 7 validadores adicionales
# Cada uno detecta un error específico de ARCA sin usar IA.
# Ref. PDFs: Ingreso-liq-La-cantidad-de-registros-04, Ingreso-liq-Long_incorrecta,
#            Generación-F931_valores_negativos, BugH (rem>10M), Validar-Liq-Cnp-SAC,
#            Ingreso-liq-La-linea-1-nro-liq, Validar-Liq-Dif_calculo_rem
# ---------------------------------------------------------------------------

def tool_validar_conteo_reg04_en_reg01(ruta: str) -> dict:
    """
    VAL-01 — Verifica que la cantidad de REG04 declarada en REG01 (pos 30-35)
    coincida con la cantidad real de líneas tipo 04 en el archivo.
    Ref: Ingreso-liq-La-cantidad-de-registros-04-informados-en-el-registro-01-X-no-coincide-con-los-encontrados-Y.pdf
    """
    try:
        lineas = _leer_lineas(ruta)
        reg01 = next((l for l in lineas if l.strip() and _tipo(l) == '01'), None)
        if not reg01:
            return {"ok": False, "error": "No se encontró REG01.", "diagnostico": "FALTA REG01."}
        if len(reg01) < REG01_CANT_REG04_END:
            return {"ok": False, "error": f"REG01 demasiado corto ({len(reg01)} chars) para leer cantidad REG04.",
                    "diagnostico": "REG01 incompleto."}
        raw = reg01[REG01_CANT_REG04_START:REG01_CANT_REG04_END].strip()
        if not raw.isdigit():
            return {"ok": False, "error": f"Campo cantidad REG04 en REG01 no numérico: '{raw}'",
                    "diagnostico": "REG01 mal formado: campo cantidad de REG04 tiene caracteres no numéricos."}
        cant_declarada = int(raw)
        cant_real = sum(1 for l in lineas if l.strip() and _tipo(l) == '04')
        ok = cant_declarada == cant_real
        return {
            "ok": ok,
            "cant_declarada_en_reg01": cant_declarada,
            "cant_real_reg04":         cant_real,
            "diferencia":              cant_real - cant_declarada,
            "diagnostico": (
                f"ERROR CRÍTICO: REG01 declara {cant_declarada} REG04 pero el archivo tiene {cant_real}. "
                "ARCA rechaza por este motivo. Regenerar el TXT desde e-SUELDOS."
                if not ok else f"Conteo REG04 correcto: {cant_real}. OK."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_longitud_registros(ruta: str) -> dict:
    """
    VAL-02 — Verifica que cada línea tenga la longitud exacta según su tipo.
    Ref: Ingreso-liq-Long_incorrecta.pdf, Cnp_Long_incorrecta.pdf
    Longitudes: REG01=35, REG02=115, REG03≥51, REG04≥370, REG05=65
    """
    try:
        lineas = _leer_lineas(ruta)
        errores = []
        for i, l in enumerate(lineas, 1):
            if not l.strip():
                continue
            t = _tipo(l)
            if t not in LONGITUDES_REQUERIDAS:
                continue
            lon = len(l)
            req = LONGITUDES_REQUERIDAS[t]
            # REG03: el sufijo ".e-s" agrega 4 chars → toleramos hasta req+4
            if t == '03':
                if lon < req:
                    errores.append({"linea": i, "tipo": t, "cuil": _cuil(l),
                                    "longitud_real": lon, "longitud_requerida": req, "deficit": req - lon})
                continue
            # REG04: longitud mínima (el CBU puede extenderlo)
            if t == '04':
                if lon < req:
                    errores.append({"linea": i, "tipo": t, "cuil": _cuil(l),
                                    "longitud_real": lon, "longitud_requerida": req, "deficit": req - lon})
                continue
            # REG01, REG02, REG05: longitud exacta
            if lon != req:
                errores.append({"linea": i, "tipo": t,
                                 "cuil": _cuil(l) if t == '02' else 'N/A',
                                 "longitud_real": lon, "longitud_requerida": req, "deficit": req - lon})
        return {
            "ok": len(errores) == 0,
            "registros_longitud_incorrecta": len(errores),
            "detalle": errores[:30],
            "diagnostico": (
                f"ERROR: {len(errores)} registros con longitud incorrecta. "
                "ARCA rechaza cualquier registro que no respete el ancho fijo del layout."
                if errores else "Todas las longitudes son correctas. OK."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_valores_negativos(ruta: str) -> dict:
    """
    VAL-03 — Detecta campos monetarios con valores negativos en REG03 y REG04.
    El LSD no admite negativos: los descuentos se expresan con flag D/C en REG03.
    Ref: Generación-F931_valores_negativos.pdf
    """
    try:
        lineas = _leer_lineas(ruta)
        afectados = []
        for i, l in enumerate(lineas, 1):
            if not l.strip():
                continue
            t = _tipo(l)
            if t == '03' and len(l) >= REG03_IMP_END:
                seg = l[REG03_IMP_START:REG03_IMP_END].strip()
                if seg.startswith('-'):
                    afectados.append({"linea": i, "tipo": "REG03", "cuil": _cuil(l),
                                      "concepto": l[REG03_COD_START:REG03_COD_END].strip(),
                                      "valor": seg})
            elif t == '04' and len(l) > 13:
                parte = l[13:]
                pos = 0
                while pos + 15 <= len(parte):
                    seg = parte[pos:pos+15].strip()
                    if seg.startswith('-'):
                        afectados.append({"linea": i, "tipo": "REG04", "cuil": _cuil(l),
                                          "posicion_absoluta": 13 + pos, "valor": seg})
                        break
                    pos += 15
        return {
            "ok": len(afectados) == 0,
            "registros_con_negativo": len(afectados),
            "detalle": afectados[:30],
            "diagnostico": (
                f"ERROR: {len(afectados)} líneas con valores negativos. "
                "El LSD no admite importes negativos; los descuentos se informan con flag D en el campo Débito/Crédito del REG03."
                if afectados else "Sin valores negativos. OK."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_notacion_cientifica(ruta: str) -> dict:
    """
    VAL-06 — Detecta notación científica (ej: 1.0E7) en campos numéricos.
    Ocurre cuando la Rem. Total supera $10.000.000 (Bug H, corregido jun-2026).
    Ref: Bug H documentado — BUG-H_Rem_total_notacion_cientifica
    """
    try:
        lineas = _leer_lineas(ruta)
        afectados = []
        patron = re.compile(r'\d[eE][+\-]?\d')
        for i, l in enumerate(lineas, 1):
            if not l.strip() or _tipo(l) not in ('03', '04'):
                continue
            parte = l[13:] if len(l) > 13 else ''
            if patron.search(parte):
                afectados.append({"linea": i, "tipo": _tipo(l), "cuil": _cuil(l),
                                   "extracto": parte[:60]})
        return {
            "ok": len(afectados) == 0,
            "registros_con_notacion_cientifica": len(afectados),
            "detalle": afectados[:30],
            "diagnostico": (
                f"BUG H DETECTADO: {len(afectados)} registros con notación científica. "
                "Ocurre cuando la Rem. Total supera $10.000.000. ARCA no puede parsear '1.0E7'. "
                "Solución: actualizar e-SUELDOS (fix commit 9b279e03) y regenerar el TXT."
                if afectados else "Sin notación científica. OK."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_sac_fuera_de_periodo(ruta: str) -> dict:
    """
    VAL-07 — Detecta conceptos SAC semestrales (rango 120.000-129.999, excepto 120.003)
    en meses distintos de junio (06) y diciembre (12).
    Ref: Validar-Liq-Cnp-SAC.pdf, Validar-Liq-No-corresponde-informar-simultaneamente-SAC-semest.pdf
    """
    try:
        lineas = _leer_lineas(ruta)
        reg01 = next((l for l in lineas if l.strip() and _tipo(l) == '01'), None)
        periodo_mes = None
        periodo_str = None
        if reg01 and len(reg01) >= REG01_PERIODO_END:
            periodo_str = reg01[REG01_PERIODO_START:REG01_PERIODO_END]
            if periodo_str.isdigit():
                periodo_mes = periodo_str[4:6]
        if not periodo_mes:
            return {"ok": True, "advertencia": "No se pudo leer el mes del período desde REG01. Validación omitida."}
        if periodo_mes in ('06', '12'):
            return {"ok": True, "periodo": periodo_str, "mes": periodo_mes,
                    "diagnostico": f"Mes {periodo_mes} es de SAC — conceptos SAC semestrales permitidos."}
        afectados = []
        for i, l in enumerate(lineas, 1):
            if not l.strip() or _tipo(l) != '03' or len(l) < REG03_COD_END:
                continue
            cod = l[REG03_COD_START:REG03_COD_END].strip()
            if not cod.isdigit():
                continue
            cod_int = int(cod)
            # Rango 120.000-129.999 → códigos 1200000-1299999 (×10 pattern del sistema)
            # Excepto 120.003 (SAC proporcional) → código 1200030, permitido siempre
            if 1200000 <= cod_int <= 1299999 and cod != '1200030':
                afectados.append({"linea": i, "cuil": _cuil(l), "concepto": cod,
                                   "problema": f"SAC semestral en mes {periodo_mes} — solo permitido en mes 06 y 12"})
        return {
            "ok": len(afectados) == 0,
            "periodo": periodo_str,
            "mes": periodo_mes,
            "registros_sac_fuera_de_periodo": len(afectados),
            "detalle": afectados[:30],
            "diagnostico": (
                f"ERROR SAC: {len(afectados)} conceptos SAC semestrales en mes {periodo_mes}. "
                "Los conceptos 120.000-129.999 (excl. 120.003) solo se pueden informar en junio y diciembre. "
                "Para SAC complementaria en otro mes: usar concepto 120.003 con días correspondientes."
                if afectados else f"Sin SAC semestral fuera de período (mes {periodo_mes}). OK."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_periodo_reg01(ruta: str) -> dict:
    """
    VAL-09 — Valida que el período declarado en REG01 (pos 16-21) sea un AAAAMM válido.
    Ref: Ingreso-liq-La-linea-1-nro-liq.pdf, Ingreso-liq-periodo_obligacion.pdf
    """
    try:
        lineas = _leer_lineas(ruta)
        reg01 = next((l for l in lineas if l.strip() and _tipo(l) == '01'), None)
        if not reg01:
            return {"ok": False, "error": "No se encontró REG01.", "diagnostico": "FALTA REG01."}
        if len(reg01) < REG01_PERIODO_END:
            return {"ok": False,
                    "error": f"REG01 demasiado corto ({len(reg01)} chars).",
                    "diagnostico": "REG01 incompleto: no se puede leer el período."}
        periodo   = reg01[REG01_PERIODO_START:REG01_PERIODO_END]
        tipo_envio       = reg01[13:15]   if len(reg01) >= 15  else '??'
        nro_presentacion = reg01[22:27]   if len(reg01) >= 27  else '?????'
        cuit_empleador   = reg01[2:13]    if len(reg01) >= 13  else '???????????'
        errores = []
        if not periodo.isdigit():
            errores.append(f"Período '{periodo}' no numérico")
        else:
            anio, mes = int(periodo[:4]), int(periodo[4:])
            if not (1 <= mes <= 12):
                errores.append(f"Mes '{periodo[4:]}' inválido (debe ser 01-12)")
            if anio < 2020:
                errores.append(f"Año '{periodo[:4]}' anterior a 2020 — posible error")
            if anio > 2030:
                errores.append(f"Año '{periodo[:4]}' mayor a 2030 — posible error")
        if tipo_envio not in ('SJ', 'RE'):
            errores.append(f"Tipo de envío '{tipo_envio}' inválido (debe ser 'SJ' o 'RE')")
        if not nro_presentacion.isdigit():
            errores.append(f"N° de presentación '{nro_presentacion}' no numérico")
        return {
            "ok": len(errores) == 0,
            "periodo": periodo,
            "cuit_empleador": cuit_empleador,
            "tipo_envio": tipo_envio,
            "nro_presentacion": nro_presentacion,
            "errores": errores,
            "diagnostico": (
                f"ERROR EN REG01: {'; '.join(errores)}. El archivo será rechazado por ARCA."
                if errores else
                f"REG01 válido. Período {periodo}, CUIT {cuit_empleador}, envío '{tipo_envio}', pres. N°{nro_presentacion}."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_rem_bruta_reg04(ruta: str) -> dict:
    """
    VAL-05 — Verifica coherencia entre Rem. Bruta (pos 161-175) y Base 1 (pos 176-190) en REG04.
    Base 1 no puede superar la Rem. Bruta, y si Rem. Bruta = 0 con bases ≠ 0 indica error de exportación.
    Ref: Validar-Liq-Dif_calculo_rem.pdf, ticket #27571 (Base Imponible > Remuneración Bruta)
    """
    try:
        lineas = _leer_lineas(ruta)
        afectados = []
        for i, l in enumerate(lineas, 1):
            if not l.strip() or _tipo(l) != '04' or len(l) < REG04_BASE1_END:
                continue
            try:
                rem   = float(l[REG04_REM_BRUTA_START:REG04_REM_BRUTA_END].strip() or '0')
                base1 = float(l[REG04_BASE1_START:REG04_BASE1_END].strip() or '0')
            except ValueError:
                continue
            if rem == 0 and base1 > 0:
                afectados.append({"linea": i, "cuil": _cuil(l),
                                   "rem_bruta": l[REG04_REM_BRUTA_START:REG04_REM_BRUTA_END].strip(),
                                   "base1": l[REG04_BASE1_START:REG04_BASE1_END].strip(),
                                   "problema": "Rem. Bruta = 0 pero Base 1 SIPA ≠ 0 (error de exportación)"})
            elif base1 > rem > 0:
                afectados.append({"linea": i, "cuil": _cuil(l),
                                   "rem_bruta": l[REG04_REM_BRUTA_START:REG04_REM_BRUTA_END].strip(),
                                   "base1": l[REG04_BASE1_START:REG04_BASE1_END].strip(),
                                   "problema": "Base 1 SIPA > Rem. Bruta — la base no puede superar la remuneración"})
        return {
            "ok": len(afectados) == 0,
            "reg04_con_problema_rem_bruta": len(afectados),
            "detalle": afectados[:30],
            "diagnostico": (
                f"ERROR: {len(afectados)} REG04 con Rem. Bruta incoherente respecto a Base 1 SIPA. "
                "Indica error en la exportación (ticket #27571). Recalcular desde e-SUELDOS."
                if afectados else "Rem. Bruta y Base 1 coherentes en todos los REG04. OK."
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}



# Gemini usa el formato de google.genai.types.FunctionDeclaration
# ---------------------------------------------------------------------------

from google.genai import types as gtypes

def _tool_def(name: str, description: str) -> gtypes.FunctionDeclaration:
    """Helper: todas las tools de validación reciben solo 'ruta'."""
    return gtypes.FunctionDeclaration(
        name=name,
        description=description,
        parameters=gtypes.Schema(
            type="OBJECT",
            properties={"ruta": gtypes.Schema(type="STRING", description="Ruta al archivo TXT de LSD")},
            required=["ruta"],
        ),
    )

TOOL_DECLARATIONS = [
    _tool_def("info_archivo",                    "Obtiene información básica del archivo LSD: tamaño, líneas totales y distribución de registros por tipo (REG01 a REG05). Llamar primero."),
    _tool_def("validar_estructura",              "Valida la estructura: exactamente 1 REG01 y 1 REG05, que todo CUIL en REG03/REG04 tenga su REG02, y que no haya tipos desconocidos."),
    _tool_def("validar_periodo_reg01",           "VAL-09: Valida que el período en REG01 (pos 16-21) sea AAAAMM válido, y verifica tipo de envío y N° de presentación."),
    _tool_def("validar_conteo_reg04_en_reg01",   "VAL-01: Verifica que la cantidad de REG04 declarada en REG01 (pos 30-35) coincida exactamente con los REG04 reales. ARCA rechaza si difieren."),
    _tool_def("validar_longitud_registros",      "VAL-02: Verifica que cada línea tenga la longitud exacta de su tipo (REG01=35, REG02=115, REG03≥51, REG04≥370, REG05=65). ARCA rechaza longitudes incorrectas."),
    _tool_def("validar_duplicados_reg04",        "Detecta Bug A: CUILs con más de un REG04 (empleado con dos legajos activos). ARCA suma todos → bases duplicadas (ratio 2,0x)."),
    _tool_def("validar_comas_numericos",         "Detecta Bug B (#30999): comas en campos numéricos de REG03/REG04. Formato correcto para ARCA es punto decimal sin separador de miles."),
    _tool_def("validar_valores_negativos",       "VAL-03: Detecta valores negativos en campos monetarios de REG03/REG04. El LSD no admite negativos; los descuentos se expresan con flag D/C."),
    _tool_def("validar_notacion_cientifica",     "VAL-06: Detecta notación científica (1.0E7) en campos numéricos. Bug H: ocurre cuando Rem. Total > $10.000.000. ARCA no puede parsear ese formato."),
    _tool_def("validar_bases_reg04",             "Detecta Bug C (Guía N°45): REG04 con Base4=0 y Base10 con valor → concepto 560.000 en lugar de 570.000 para docentes No-SIPA."),
    _tool_def("validar_rem_bruta_reg04",         "VAL-05: Verifica coherencia entre Rem. Bruta (pos 161-175) y Base 1 SIPA (pos 176-190) en REG04. La base no puede superar la remuneración."),
    _tool_def("validar_cbu",                     "Detecta CBU con formato incorrecto en REG04. Debe ser exactamente 22 dígitos numéricos."),
    _tool_def("analizar_conceptos_reg03",        "Analiza conceptos ARCA en REG03: top-10, detección del concepto obsoleto 560.000 (Guía 45), conceptos presentes/faltantes."),
    _tool_def("validar_sac_fuera_de_periodo",    "VAL-07: Detecta conceptos SAC semestrales (120.000-129.999 excl. 120.003) en meses distintos de junio y diciembre. ARCA los rechaza."),
]

TOOL_FUNCTIONS = {
    "info_archivo":                  tool_info_archivo,
    "validar_estructura":            tool_validar_estructura,
    "validar_periodo_reg01":         tool_validar_periodo_reg01,
    "validar_conteo_reg04_en_reg01": tool_validar_conteo_reg04_en_reg01,
    "validar_longitud_registros":    tool_validar_longitud_registros,
    "validar_duplicados_reg04":      tool_validar_duplicados_reg04,
    "validar_comas_numericos":       tool_validar_comas_numericos,
    "validar_valores_negativos":     tool_validar_valores_negativos,
    "validar_notacion_cientifica":   tool_validar_notacion_cientifica,
    "validar_bases_reg04":           tool_validar_bases_reg04,
    "validar_rem_bruta_reg04":       tool_validar_rem_bruta_reg04,
    "validar_cbu":                   tool_validar_cbu,
    "analizar_conceptos_reg03":      tool_analizar_conceptos_reg03,
    "validar_sac_fuera_de_periodo":  tool_validar_sac_fuera_de_periodo,
}

# Agregar tools de errores ARCA si están disponibles
# TOOLS_ERRORES viene en formato Anthropic dict → convertir a FunctionDeclaration
TOOL_DECLARATIONS_ERRORES = []
for t in TOOLS_ERRORES:
    props = {}
    for pname, pdef in t["input_schema"].get("properties", {}).items():
        props[pname] = gtypes.Schema(type="STRING", description=pdef.get("description", ""))
    TOOL_DECLARATIONS_ERRORES.append(gtypes.FunctionDeclaration(
        name=t["name"],
        description=t["description"],
        parameters=gtypes.Schema(
            type="OBJECT",
            properties=props,
            required=t["input_schema"].get("required", []),
        ),
    ))

TOOL_DECLARATIONS_ALL = TOOL_DECLARATIONS + TOOL_DECLARATIONS_ERRORES
TOOL_FUNCTIONS_ALL     = {**TOOL_FUNCTIONS, **TOOL_FUNCTIONS_ERRORES}

PRECHECKS = [
    # Bloque 1 — Estructura básica (sin leer campos internos)
    "info_archivo",
    "validar_periodo_reg01",
    "validar_estructura",
    "validar_conteo_reg04_en_reg01",
    "validar_longitud_registros",
    # Bloque 2 — Errores de formato en campos numéricos
    "validar_comas_numericos",
    "validar_valores_negativos",
    "validar_notacion_cientifica",
    # Bloque 3 — Validaciones de negocio
    "validar_duplicados_reg04",
    "validar_bases_reg04",
    "validar_rem_bruta_reg04",
    "validar_cbu",
    "analizar_conceptos_reg03",
    "validar_sac_fuera_de_periodo",
]


# ---------------------------------------------------------------------------
# System prompt (idéntico al original)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Eres un experto en el módulo "LSD Nuevo" (Libro Sueldo Digital v2) del sistema e-SUELDOS
y en validación de archivos TXT de LSD para ARCA/AFIP Argentina.

Podés responder preguntas sobre la lógica interna del módulo Y analizar archivos TXT para
detectar errores antes de presentar a ARCA.

═══════════════════════════════════════════════════════════════
ARQUITECTURA: MÓDULO LSD NUEVO (e-SUELDOS)
═══════════════════════════════════════════════════════════════
El módulo LSD Nuevo es la versión v2 del exportador de Libro Sueldo Digital.
A diferencia del módulo viejo (solo exporta TXT), el LSD Nuevo tiene:

  INTERFAZ (front-end):
  - Modal con múltiples pestañas: Resumen de empleados, Conceptos por empleado,
    Totales, y modal de Exportación.
  - Campos editables por empleado: tipo de empresa, modalidad CCT, CBU, forma de pago,
    bases adicionales, aportes OS, contribuciones OS, marca reducción, etc.
  - Botones: Listar, Recalcular, Exportar LSD (TXT), Exportar F931.
  - Exportación pide: N° de Presentación (default 1), Identificación de Envío,
    Fecha de Pago (DD/MM/AAAA), Jurisdicción.

  CLASES JAVA PRINCIPALES:
  - ControladorLibroDigitalNuevo.java — Controlador Struts; maneja acciones:
      listarResumen, recalcular, guardarResumen, exportarLibroDigital, exportar931,
      obtenerConceptos, listarCondicion, listarRevista, listarModalidad, etc.
  - ExpertoReportesLiquidacion.java — Orquestador; selecciona el Totpago (liquidación)
      del período y delega en la estrategia de cálculo.
      Para LSD: prefiere tipo M (mensual) como header; fallback a cualquier tipo del período.
  - EstrategiaReporteLibroDigital.java — Motor de cálculo; genera REG01 a REG05.
      Métodos clave: calcular(), calcularRegistrosTipo04(), limpiar().
  - EstrategiaReporte931.java — Motor del Form 931 (análogo).

  BASE DE DATOS:
  - Tabla `reportesliquidacion` — cabecera del reporte (mes, anio, empresa, fechareporte).
  - Tablas `librodigitalreg01` a `librodigitalreg05` — datos calculados, vinculados al reporte.
  - Tabla `totalesliquidacion` — totales del reporte (legajos, bases, aportes, etc.).
  - Tabla `calculo` — datos fuente; clave compuesta: empresa+legajo+concepto+fecha+mes+periodo+tipo.
  - Tabla `modpre` — valores MOPRE por período y trimestre.
  - Tabla `regcontr` — modalidades de contratación con campo DETRACCION_03 (detracción mensual).

  FLUJO RECALCULAR:
  1. limpiar() → borra REG01-REG05 del reporte existente, fuerza forzarCalculo=true.
  2. listarResumen() → llama calcular() → genera todos los registros desde tabla `calculo`.
  3. calcularRegistrosTipo04(): itera legajos de la empresa; por cada legajo:
     a. Crea DaoCalculo nuevo (dentro del loop, no fuera — fix empresa 8403).
     b. Filtra por mes+periodo y tipo31 in (M,F,P,V,S,O) — incluye todos los tipos activos.
     c. Acumula conceptos clave (903, 913, 992, 993, 994, etc.) y bases por denominación.
     d. Aplica reglas MOPRE → calcula Rem10 (Base10) e ImporteDetraccion.
     e. Guarda LibroDigitalReg04 en BD.

═══════════════════════════════════════════════════════════════
CONOCIMIENTO: TABLA MOPRE (ModPre) — VALORES 2026
═══════════════════════════════════════════════════════════════
La tabla `modpre` tiene: id, periodo (año), trimestre (=mes), minModPre, maxModPre, sac, smvm.

Valores reales 2026 (trimestre = mes):
  Mes 1:  min=117.643,93  max=3.823.372,92  sac=1.911.686,37
  Mes 2:  min=120.996,78  max=3.932.339,08  sac=1.966.169,54
  Mes 3:  min=124.481,49  max=4.045.590,45  sac=2.022.795,22
  Mes 4:  min=128.091,45  max=4.162.912,57  sac=2.081.456,28
  Mes 5:  min=132.420,94  max=4.303.619,01  sac=2.151.809,50
  Mes 6:  min=132.420,94  max=4.303.619,01  sac=2.151.809,50

NOTA CRÍTICA sobre el campo `sac`:
  El campo `sac` (~2.15M) es el componente SAC del TECHO MÁXIMO de aportes
  (≈ maxModPre / 2). NO es un ajuste al umbral mínimo de la detracción.
  En meses de SAC (6 y 12):
    - Techo para Bases 1/4/5 = maxModPre + sac (1.5x el máximo normal) ✓ CORRECTO
    - Umbral para detracción  = minModPre SIN sumar sac               ✓ CORRECTO
  ERROR HISTÓRICO (corregido jun-2026): al sumar sac al umbral de detracción
  para mes 6/12 → umbral = 132.420 + 2.151.809 = 2.284.230 → cualquier empleado
  con sueldo < 2.28M quedaba sin detracción (ej: empresa 3333, legajo 13, sueldo 1M).

═══════════════════════════════════════════════════════════════
CONOCIMIENTO: CÁLCULO DETRACCIÓN / REM10 (ticket #31049)
═══════════════════════════════════════════════════════════════
La detracción es una deducción AFIP aplicada a la base imponible de altos ingresos.
En el LSD se refleja como:
  - ImporteDetraccion → campo en REG04
  - Base10 (Rem10) = Base2 - ImporteDetraccion → campo en REG04

REGLAS DE APLICACIÓN:
  1. Umbral de activación: rem > minModPre (L142). El umbral es FIJO, no varía por mes.
     - Si Base2 < minModPre → ImporteDetraccion = 0, Base10 = 0.
     - Si Base2 >= minModPre → se calcula detracción.

  2. Fuente de la detracción (en orden de prioridad):
     a. Concepto 903 (detracción mensual liquidada) + Concepto 913 (detracción SAC liquidada).
     b. Si 903+913 = 0 → fallback: tabla regcontr (modalidad de contratación), campo DETRACCION_03.
        - En meses de SAC (cuando sac > 0 en la liquidación): se suma 50% extra.
          detraccionInicial = detraccion03 + (detraccion03 * 0.5)
        - En meses normales: detraccionInicial = detraccion03.

  3. Cálculo final:
     rem2MenosDetraccion = Base2 - detraccionInicial
     Si rem2MenosDetraccion < minModPre:
       → Base10 (Rem10) = minModPre
       → ImporteDetraccion = max(0, Base2 - minModPre)
     Si rem2MenosDetraccion >= minModPre:
       → Base10 (Rem10) = rem2MenosDetraccion
       → ImporteDetraccion = detraccionInicial

BASES IMPONIBLES 1, 4 y 5 — TOPE CON maxModPre:
  - Base1, Base4, Base5 se acumulan de los conceptos mapeados por denominación.
  - Están topeadas: si valor > maxModPre → se limita a maxModPre.
  - En meses 6/12 (SAC): el tope es maxModPre + sac (= 1.5x el máximo normal).
  - Base2, Base3, Base6, Base7, Base8, Base9 NO tienen tope de MOPRE.

CONCEPTOS CLAVE INTERNOS (no son códigos ARCA):
  903 → detracción mensual (mapeado a denominación "21" en codliq)
  913 → detracción SAC     (mapeado a denominación "21" en codliq)
  993 → Rem. total SS (base para calcular historico993)
  992, 994 → otros totalizadores
  999 → concepto totalizador general

CASOS DE FALLA CONOCIDOS Y CORREGIDOS:
  Caso 5 (corregido): Empleado con sueldo X donde minModPre < X < maxModPre no tenía
    detracción porque el sistema comparaba con maxModPre (~4.16M) en lugar de minModPre (~132K).
    Fix: cambiar L143 (max) por L142 (min) como umbral de activación.

  Caso 7.1 (corregido): SAC complementaria en mes distinto de 6/12 → detracción = 0.
    Causa: la condición mes==6 OR mes==12 bloqueaba el ajuste del 50% del SAC para otros meses.
    Fix: cambiar a "si hay SAC en la liquidación (sac > 0)" → aplica en cualquier mes.

  Mes 6 (corregido jun-2026): Empresa 3333 sin detracción en jun-2026 con sueldo 1M.
    Causa: topeMopreConver = minModPre + sac = 132K + 2.15M = 2.28M → 1M < 2.28M → sin detracción.
    Fix: topeMopreConver = solo minModPre, sin sumar sac.

═══════════════════════════════════════════════════════════════
CONOCIMIENTO: SELECCIÓN DE LIQUIDACIÓN (Totpago)
═══════════════════════════════════════════════════════════════
Una empresa puede tener múltiples tipos de liquidación en el mismo mes:
  M = Mensual (principal)
  F = Final / especiales
  O = Otros
  P, V, S = Proporcionales, Vacaciones, Sueldo anual, etc.

El LSD incluye a TODOS los empleados de TODOS los tipos activos del período
(tipo31 in M, F, P, V, S, O). NO filtra por fecha de liquidación específica.

El "header" del LSD usa el Totpago tipo M del período.
  - Si no hay tipo M → usa cualquier tipo disponible (fallback).
  - Bug histórico (corregido): cuando había tipos F+M, se tomaba F como header
    y se filtraba por fecha de F → solo aparecían los legajos de F.

CASO EMPRESA 8403 (corregido may-2026):
  Empresa con tipos F (01/04, 1 legajo), M (30/04, 105 legajos), O (22/04, 110 legajos).
  Bug anterior: DaoCalculo creado fuera del loop → filtros se apilaban entre legajos.
  Bug anterior: filtrarPorFecha(fecha de F) → solo 1 legajo aparecía de 105+.
  Fix: DaoCalculo nuevo dentro del loop + eliminación de filtrarPorFecha.

═══════════════════════════════════════════════════════════════
CONOCIMIENTO: FORMATO LSD (TXT AFIP)
═══════════════════════════════════════════════════════════════
El LSD es un archivo de texto de ancho fijo con los siguientes tipos de registro:

  REG01 — Cabecera del archivo (exactamente 1, primera línea)
           Contiene: CUIT empleador, período AAAAMM, tipo de envío,
           N° de presentación (5 dígitos, default "00001"), fecha de pago (AAAAMMDD),
           identificación de envío.

  REG02 — Cabecera por empleado (1 por CUIL, aunque tenga múltiples legajos)
           Posición 2-12: CUIL del empleado (11 dígitos)
           Contiene: nombre, CUIL, CBU, forma de pago, tipo de empresa, etc.

  REG03 — Conceptos remunerativos (N por empleado)
           Posición 2-12:  CUIL
           Posición 13-19: código ARCA (7 dígitos, ej: 5600000 = concepto 560.000)
           Posición 20-35: importe (punto decimal, sin separador de miles)
           Sufijo: ".e-s" al final de cada REG03 (identificador del sistema e-SUELDOS)

  REG04 — Bases imponibles por empleado (normalmente 1 por CUIL)
           Posición 2-12:  CUIL
           Posición ~220-234: Base 4 (aportes OS/FSR) — topeada con maxModPre
           Posición ~340-354: Base 10 = Rem10 (Ley 27.430) — Base2 - ImporteDetraccion
           Final del registro: CBU (22 dígitos) + Forma de pago

  REG05 — Registro de cierre/totales (exactamente 1, última línea)

Los campos numéricos NO deben contener comas; el formato correcto es punto decimal
sin separador de miles: "1000.50" (no "1.000,50").

═══════════════════════════════════════════════════════════════
CONOCIMIENTO: BUGS CONOCIDOS DEL SISTEMA
═══════════════════════════════════════════════════════════════

BUG A — REG04 DUPLICADO (empleado con dos legajos)
  Causa:    El generador de LSD itera por legajo y produce un REG04 por legajo,
            sin consolidar empleados con el mismo CUIL.
  Efecto:   ARCA suma todos los REG04 del mismo CUIL → bases duplicadas (ratio 2,0x).
            8 CUILs extra pueden quedar con REG04 vacío (bases = 0) → diferencia inversa.
  Detección: CUILs con más de 1 registro REG04 en el archivo.
  Solución:  Corrección en código Java: consolidar REG04 por CUIL (como ya se hace en REG02).
  Impacto típico: 142+ CUILs afectados con patrón de ratio exacto 2,0.

BUG B — COMA EN CAMPOS NUMÉRICOS (interno #30999)
  Causa:    El campo "sinmascaraDec" no estaba definido en el jQuery plugin mascaraHandler.js.
            Cuando el usuario ingresaba "100,00" (formato argentino), el sistema guardaba
            ese valor con coma en la base de datos. Al exportar, quedaba "100,00" en el TXT.
  Efecto:   ARCA rechaza el registro porque los campos numéricos deben tener punto (no coma).
  Detección: Comas en posición 13+ de REG03/REG04.
  Solución:  Corrección ya aplicada en el sistema (v fix #30999). Regenerar el LSD.

BUG C — CONCEPTO 560.000 VS 570.000 (Guía N°45 ARCA, dic-2025)
  Contexto: La Guía N°45 "LSD: Docentes No SIPA" (29/12/2025) establece nuevos requisitos
            para docentes que tributan al régimen previsional provincial (IPS) y no al SIPA.
            A partir del primer trimestre 2026, ARCA activó validación cruzada REG03 vs REG04.

  Con 560.000 (incorrecto para docentes No-SIPA desde Guía 45):
    Base 1/2/3 (SIPA)    → 0
    Base 4 (aportes OS)  → 0  (NO se informa — INCORRECTO)
    Base 8 (contrib. OS) → 0  (NO se informa — INCORRECTO)
    Base 10 (Ley 27.430) → importe (acumula INCORRECTAMENTE)

  Con 570.000 (correcto según Guía 45):
    Base 1/2/3 (SIPA)    → 0
    Base 4 (aportes OS)  → valor real (OBLIGATORIO)
    Base 8 (contrib. OS) → valor real (OBLIGATORIO)
    Base 9 (LRT)         → activo por defecto
    Base 10 (Ley 27.430) → 0 (OBLIGATORIO)

  Conceptos relacionados Guía 45 a dar de alta:
    570.001 — SAC No Contributivo
    570.002 — SAC Proporcional No Contributivo
    570.003 — Vacaciones No Contributivo
    810.015 — Descuento sistema previsional no nacional
    810.016 — Descuento obra social provincial

  Solución: Solo configuración (no requiere cambios de código):
    1. Dar de alta concepto 570.000 con idDenominación correcto → Base 4, 8, 9.
    2. Remapear el concepto interno de la empresa de 560.000 a 570.000.
    3. Evaluar 570.001/002/003 y 810.015/016 según corresponda.

BUG D — DETRACCIÓN = 0 EN MES 6/12 (corregido jun-2026)
  Causa:    El umbral de detracción se calculaba como minModPre + sacModPre para meses 6 y 12.
            El campo `sac` de la tabla modpre es el componente SAC del techo máximo (~2.15M),
            NO un ajuste al umbral mínimo. Al sumarlo: umbral = 132K + 2.15M = 2.28M.
  Efecto:   Todo empleado con sueldo < 2.28M en junio o diciembre aparecía sin detracción.
            Base10 (Rem10) = 0, ImporteDetraccion = 0.
  Detección: En archivo de mes 6 o 12, todos los REG04 con Rem10 y Detracción en 0
             a pesar de tener bases significativas.
  Solución:  Corrección en EstrategiaReporteLibroDigital.java (fix corregido jun-2026).
             Regenerar el LSD con Recalcular.

BUG E — SOLO 1 LEGAJO DE MUCHOS (empresa con múltiples tipos de liquidación)
  Causa:    DaoCalculo creado fuera del loop de legajos → filtros se apilaban.
            Adicionalmente: se filtraba por fecha del Totpago M, excluyendo F y O.
  Efecto:   El LSD mostraba 1 legajo en lugar de los 100+ liquidados.
  Detección: En front del LSD Nuevo: resumen con 1 legajo cuando hay muchos liquidados.
  Solución:  Corrección aplicada (may-2026). Regenerar con Recalcular.

BUG F — DETRACCIÓN = 0 PARA SAC COMPLEMENTARIA EN MES NO 6/12
  Causa:    El ajuste del 50% del SAC en la detracción estaba condicionado a mes==6 OR mes==12.
            El SAC complementaria puede ocurrir en cualquier mes.
  Efecto:   Empleados con SAC complementaria fuera de junio/diciembre sin detracción.
  Solución:  Corrección aplicada. La condición ahora es "si hay SAC en la liquidación"
             (sac > 0), independientemente del mes.

BUG G — DETRACCIÓN SAC INCOMPLETA CUANDO M+S EN MISMO PERÍODO (corregido jun-2026)
  Causa:    El ajuste del 50% SAC solo se aplicaba dentro del bloque "if (detraccionInicial==0)".
            En empresas con tipo M y tipo S en el mismo período: los conceptos 903+913 del tipo M
            ya producen detraccionInicial != 0 → se salteaba el bloque → el 50% SAC nunca se sumaba.
  Efecto:   detraccionInicial = solo la parte mensual (ej: 7003.68) en vez de 1.5x (10505.52).
            La detracción correcta = base + base*0.5 = base*1.5.
  Detección: En un período con tipo M + tipo S: Importe Detraer en REG04 = solo el mensual,
             Base10 (Rem10) desplazada por no incorporar el SAC en la detracción.
  Solución:  El ajuste SAC se movió FUERA del bloque if=0. Ahora aplica siempre que sac>0,
             independientemente del origen de detraccionInicial (liquidación o tabla).
             Fórmula: if (sac > 0 && detraccionInicial > 0) → detraccionInicial *= 1.5
  Commit:   34118fe3 (jun-2026)

BUG H — REM TOTAL > 10.000.000 MUESTRA NOTACIÓN CIENTÍFICA (corregido jun-2026)
  Causa:    remuneracionTotal era Double. Double.toString() usa notación científica para valores
            >= 10^7: Double.toString(10_000_000.5) → "1.00000005E7".
            Este string se guardaba en DB y se mostraba en el front y en el TXT.
  Efecto:   En el front del LSD Nuevo: el campo "Rem. Total" muestra "1.0E7" en vez del número.
            En el TXT: campo de longitud fija con "1.0E7" que ARCA no puede parsear.
  Afecta:   Solo empleados con remuneración bruta > $10.000.000.
            Las bases 1-10 no se afectan (están topeadas con maxModPre ~4.3M).
  Solución: BigDecimal.valueOf(remuneracionTotal).setScale(2, RoundingMode.HALF_UP).toPlainString()
            Garantiza siempre "10000000.50" sin notación científica.
  Commit:   9b279e03 (jun-2026)

BUG I — DÍAS Y HORAS SIMULTÁNEOS EN EL TXT (corregido jun-2026)
  Causa:    EstrategiaReporteLibroDigital acumulaba cantidadDiasTrabajados (unid15='D') y
            horasTrabajadas (unid15='H') de todos los conceptos del período.
            Si el empleado tenía conceptos de ambos tipos, AMBOS valores quedaban != 0.
  Efecto:   ARCA rechaza REG04 con días y horas simultáneamente no-cero.
            AFIP spec: informar SOLO días O horas, no ambos.
  Solución: Si cantidadDiasTrabajados > 0 → setHorasTrabajadas("0") y usar días.
            Si no → setDiasTrabajados("0") y usar horas.
            El front (libroDigitalNuevoModal.js) también aplica exclusión mutua en el editor.
  Commit:   9b279e03 (jun-2026)

BUG J — REM TOTAL NO SE GUARDABA DESDE EL FRONT (corregido jun-2026)
  Causa:    En libroDigitalNuevoModal.js, la función _guardar() construye el param manualmente.
            El campo "remuneracionbrutaexp" (txt_remtotal) nunca estaba incluido en el param.
            Tampoco estaba en setCampos(), por lo que frm.txt_remtotal era undefined.
  Efecto:   El usuario podía editar la Rem. Total en el modal y guardar, pero el valor
            no se enviaba al backend y siempre volvía al valor calculado original.
  Solución: txt_remtotal agregado a setCampos(). En _guardar():
            'resumen.remuneracionbrutaexp': frm.txt_remtotal.sinmascaraDec()
            El backend (guardarResumen en ExpertoReportesLiquidacion.java) ya lo tenía implementado.
  Commit:   9b279e03 (jun-2026)

BUG K — BASE10 (REM10) INFORMADA CUANDO NO HAY DETRACCIÓN (corregido jun-2026)
  Causa:    En las reglas MOPRE, la condición de entrada al cálculo era solo
            "if (baseImponible2 < topeMopreConver)". Cuando detraccionInicial=0
            (empleado sin zona desfavorable, sin entry en regcontr) y rem >= minMOPRE,
            el código caía en el else final: base10 = baseImponible2, detraccion = 0.
  Efecto:   Empleados sin zona desfavorable (no aplica detracción) tenían Base10 = Base2
            en el TXT. ARCA valida que si no hay detracción, Base10 debe ser 0.
  Regla AFIP: Base10 (Remuneración neta de detracción) solo se informa cuando HAY detracción
              aplicable. Si detraccionInicial=0 → Base10=0, ImporteDetraer=0.
  Solución: Condición combinada al inicio del bloque MOPRE:
            if (detraccionInicial == 0d || baseImponible2 < topeMopreConver) → base10=0, detr=0
  Commit:   ecbc99e1 (jun-2026)

═══════════════════════════════════════════════════════════════
ESPECIFICACIÓN TÉCNICA COMPLETA DE REGISTROS (SPEC-LSD-2025)
═══════════════════════════════════════════════════════════════
Fuente: documento interno SPEC-LSD-2025-REFAC + layouts CSV LSD v5.
Codificación obligatoria del TXT: ANSI (Windows-1252). NO usar UTF-8.
Fin de línea: CRLF (\r\n).

REGLAS DE FORMATO (transversales):
  NUMÉRICO (N): alineado a derecha, relleno con '0'. PROHIBIDO usar espacios. NULL=ceros.
  ALFANUMÉRICO (A): alineado a izquierda, relleno con espacios. Mayúsculas obligatorias.
                    Sanitizar: Ñ→N, Á→A, tildes→sin tilde. Eliminar símbolos (º,°,ª,",.).
  MONEDA ($): numérico en centavos, sin punto ni coma. $1050.50 → 0000000105050.

LONGITUDES EXACTAS POR REGISTRO:
  REG01 = 35 chars    REG02 = 115 chars    REG03 = 51 chars
  REG04 = 370 chars   REG05 = 65 chars

REGISTRO 01 — Encabezado (35 chars):
  Pos  1- 2:  Identificador "01"
  Pos  3-13:  CUIT empleador (11 dígitos, sin guiones)
  Pos 14-15:  Id. Envío: "SJ"=Sueldos y Jornales / "RE"=Rectificativa
  Pos 16-21:  Período AAAAMM
  Pos 22:     Tipo liquidación: M=Mensual, Q=Quincena, D=Días, H=Horas
              (Si RE: espacio en blanco)
  Pos 23-27:  N° Presentación (5 dígitos, default 00001)
  Pos 28-29:  Días base (generalmente 30)
  Pos 30-35:  Cant. REG04 (CRÍTICO: debe coincidir exactamente con la cantidad generada)

REGISTRO 02 — Datos empleado (115 chars):
  Pos  1- 2:  Identificador "02"
  Pos  3-13:  CUIL empleado (sin guiones)
  Pos 14-23:  Legajo interno (A, 10 chars) — en multi-legajo: el de mayor antigüedad
  Pos 24-73:  Dependencia/Sector (A, 50 chars)
  Pos 74-95:  CBU (N, 22 dígitos) — si pago=efectivo/cheque: 0000000000000000000000
  Pos 96-98:  Días tope para proporcionar MOPRE (000 = 30 días por defecto)
  Pos 99-106: Fecha pago AAAAMMDD
  Pos 107-114: Fecha rúbrica — completar con 8 espacios (no se usa)
  Pos 115:    Forma de pago: 1=Efectivo, 2=Cheque, 3=Acreditación en cuenta

REGISTRO 03 — Conceptos (51 chars):
  Pos  1- 2:  Identificador "03"
  Pos  3-13:  CUIL empleado
  Pos 14-23:  Código concepto ARCA (A, 10 chars) — debe estar parametrizado en ARCA
  Pos 24-28:  Cantidad (N, 5 chars: 3 enteros + 2 decimales implícitos). 30días=03000
  Pos 29:     Unidades: D=Días, H=Horas, %=Porcentaje, $=Pesos, (espacio)=sin unidad
  Pos 30-44:  Importe (N, 15 chars: 13 enteros + 2 decimales implícitos). Siempre positivo.
  Pos 45:     Débito/Crédito: C=Crédito (pago al empleado) / D=Débito (descuento)
  Pos 46-51:  Período ajuste AAAAMM (si retroactivo) o 6 espacios (concepto normal del mes)
  Sufijo: cada REG03 termina con ".e-s" (identificador e-SUELDOS, campo @Transient)

  MULTI-LEGAJO (crítico): deben incluirse los conceptos de TODOS los legajos del CUIL en el período.

REGISTRO 04 — Bases F931 (370 chars):
  Pos  1- 2:  Identificador "04"
  Pos  3-13:  CUIL
  Pos 14:     Cónyuge (1=Sí, 0=No)
  Pos 15-16:  Cantidad hijos
  Pos 17:     Marca CCT (1=Sí, 0=No)
  Pos 18:     Marca SCVO (1=Sí, 0=No)
  Pos 19:     Reducción contribuciones patronales (0 o 1 — BUG: salía "T", corregido)
  Pos 20:     Tipo empresa (generalmente 1=privada)
  Pos 21:     Tipo operación (generalmente 0)
  Pos 22-35:  Códigos situación revista (14 chars: activo/licencia/zona/modalidad)
  Pos 48-50:  Días trabajados (N, 3 chars) — MAX(dias_leg1, dias_leg2); BUG: el CSV decía 2
              chars pero ARCA requiere 3. Relleno: 030. MES COMPLETO: forzar 030.
  Pos 53-57:  % Aporte Adicional SS (N, 5 chars) — BUG: salían espacios, debe ser 00000
  Pos 63-68:  Código Obra Social (A, 6 chars) — padLeft con ceros: 001102. Sin OS: 000000
  Pos 69-70:  Cantidad adherentes OS
  Pos 71-85:  Aporte adicional OS (N, 15)
  Pos 86-100: Contribución adicional OS (N, 15)
  Pos 101-160: Bases diferenciales jornada reducida (Guía G14): 4 campos de 15 chars
  Pos 161-175: Remuneración Bruta (N, 15) — suma de todos los remunerativos de todos los legajos
  Pos 176-190: Base Imp. 1 SIPA (N, 15) — MIN(RemBruta, TOPE_MAXIMO_SIPA) — topeada con maxModPre
  Pos 191-205: Base Imp. 2 Contribuciones (N, 15) — ídem Base 1
  Pos 206-220: Base Imp. 3 FNE (N, 15) — ídem Base 1
  Pos 221-235: Base Imp. 4 OS/FSR (N, 15) — MIN(RemBruta, TOPE_MAXIMO_OS) — topeada con maxModPre
              (en mes de SAC 6/12: tope = maxModPre + sac)
  Pos 236-250: Base Imp. 5 INSSJP (N, 15) — ídem Base 4
  Pos 251-310: Bases 6-9 (LRT, Régimen Diferencial, etc.) — 4 campos de 15 chars
  Pos 311-325: Sueldo + Adicionales (N, 15)
  Pos 326-340: SAC (N, 15)
  Pos 341-355: Horas Extras (N, 15) — BUG histórico: se sumaban a días trabajados → error ARCA
  Pos 356-370: Base Imp. 10 / Rem10 (N, 15) = Base2 - ImporteDetraccion (Ley 27.430)
  Pos 371-385: Vacaciones (N, 15)
  Pos 386-388: Días maternidad (N, 3)

  MULTI-LEGAJO (crítico): UN SOLO REG04 por CUIL.
    - Remuneración Bruta = SUM(rem_leg1 + rem_leg2 + ...)
    - Días trabajados = MAX(dias_leg1, dias_leg2) — NUNCA sumar si son simultáneos (tope: 30)
    - Bases = acumuladas de todos los legajos

REGISTRO 05 — Trabajadores eventuales (65 chars):
  Solo para empresa con empleados de modalidad 102 (eventual).
  Pos  1- 2:  "05"
  Pos  3-13:  CUIL empleado
  Pos 14-19:  Categoría profesional (N, 6)
  Pos 20-23:  Puesto desempeñado (N, 4)
  Pos 24-31:  Fecha ingreso AAAAMMDD
  Pos 32-39:  Fecha egreso AAAAMMDD
  Pos 40-54:  Remuneración (N, 15, centavos)
  Pos 55-65:  CUIT empresa de servicios eventuales (N, 11)

═══════════════════════════════════════════════════════════════
GUÍA AFIP V2.0 — CONCEPTOS CLAVE (LS_Conceptos_Basicos_V2.0)
═══════════════════════════════════════════════════════════════
Fuente: Guía oficial AFIP "Libro de Sueldos Digital — Conceptos Básicos y Guía de Uso V2.0" (Marzo 2018).

MÓDULOS DEL LSD (aplicativo AFIP):
  1. MÓDULO CONCEPTOS — Parametrización: asociar conceptos del empleador a conceptos ARCA.
     Tipos de conceptos: REMUNERATIVOS (110000-499999), NO REMUNERATIVOS, DESCUENTOS (810000-829999).
     Métodos de carga: Manual, Copia masiva, Importación de archivo.
     Regla: conceptos usados en liquidaciones NO pueden editarse ni borrarse.
     Remunerativos: TODOS los subsistemas SS deben tener valor "1" (salvo regímenes diferenciales).
     Descuentos: TODOS los subsistemas SS deben tener valor "0".

  2. MÓDULO LIQUIDACIONES Y DDJJ — Carga y validación por período.
     Estados: Blanco=sin validar, Rojo=con errores, Verde=válida.
     Etapas: Carga → Validación → Aceptación → Generación libro/F931.

  3. MÓDULO CONSULTAS.

TRATAMIENTO SAC (conceptos ARCA 120000-129999):
  - Rango 120000-129999 (excepto 120003): tope SAC completo ANSeS (base 180 días).
  - Concepto 120003 (SAC proporcional): único que permite proporcionar días en campo "cantidad".
  - Conceptos SAC (excepto 120003) SOLO pueden informarse en JUNIO y DICIEMBRE.
  - SAC complementaria en otros meses: usar 120003 con días correspondientes.

TRATAMIENTO ADELANTO VACACIONAL:
  - Concepto 150000 (adelanto vacacional): tope separado del tope mensual, sin SAC.
  - Informar días en campo "cantidad" del REG03.

ERRORES FRECUENTES DE VALIDACIÓN ARCA:
  1. Relación laboral no vigente en Simplificación Registral.
  2. Cálculo incorrecto de aportes SS/OS (porcentaje incorrecto sobre base imponible).
  3. Bases imponibles informadas ≠ bases calculadas (por diferencia en parametrización).
  4. Conceptos no parametrizados en módulo Conceptos (ARCA rechaza código desconocido).
  5. Datos obligatorios faltantes.
  6. Remuneración bruta en REG04 ≠ suma de remunerativos en REG03.

═══════════════════════════════════════════════════════════════
TICKETS DE CONSULTORÍA Y BUGS TÉCNICOS CONOCIDOS
═══════════════════════════════════════════════════════════════
Fuente: SPEC-LSD-2025-REFAC checklist + documentos internos.

#30440 (1): F931 TXT — espacios en blanco en pos 54 (% Aporte Adicional SS) → debe ser "00000"
#30440 (2): F931 TXT — código Obra Social vacío o incompleto → debe ser padLeft(codOS, 6, '0')
#30440 (3): F931 TXT — pos 227 (Reducción) muestra "T" → debe ser "1" o "0" (booleano a int)
#30440 (4): F931 TXT — datos complementarios (Sueldo, SAC, Extras, Vacaciones) en 0
            → falta acumuladores por clasificación de concepto
#30440 (5): F931 TXT — días trabajados (pos ~320) sin ceros → debe ser 3 dígitos: "030"
#30440 (6): Encoding — tildes y ñ generan error → implementar Sanitizer + forzar ANSI Win-1252
#30440 (7/8): Situación revista — días trabajados en 0 por licencia enfermedad
              → si tiene licencia paga, reportar días según normativa AFIP (dias_base)
#30467: Multi-Legajo — mismo CUIL con legajo 1 y 25 genera líneas separadas
        → implementar patrón "Nodo CUIL": agrupar por CUIL, sumar bases, MAX días
#29220: Exportación — no unifica Mensual y SAC al exportar conceptos AFIP
        → el servicio debe barrer TODAS las liquidaciones del período para el CUIL
#27571: Errores ARCA de presentación — Base Imponible > Remuneración Bruta
        → asegurar que BaseImp <= RemBruta (salvo excepciones G14)

BUG HORAS EXTRAS (Ticket Programación 27/08/2025):
  Error ARCA: "No se puede informar cant. días y cant. horas trabajadas de manera simultánea"
  Causa: horas extras se sumaban al total de horas trabajadas del mes.
  Las horas extras DEBEN informarse en campo separado (pos 341-355 REG04), NO en días/horas.
  Solución: separar acumulador de horas extras del acumulador de horas trabajadas.

BUG MULTI-TIPO-LIQUIDACIÓN (documentado en LSD NUEVO.docx):
  Síntoma: al tener primera y segunda quincena, el sistema solo valida una de ellas.
  La primera quincena no se suma al exportar el TXT → error en todas las bases.
  Si solo hay una liquidación (mensual), valida OK. Con complementaria también OK.
  Causa raíz: filtro excluyente por tipo de liquidación en la query de exportación.
  Solución: el servicio debe barrer TODOS los tipos de liquidación del período (ver #29220).
  Estado en e-SUELDOS: corregido — filtrarPorLiquidaciones() incluye M,F,P,V,S,O;
  se eliminó filtrarPorFecha() que restringía a solo un tipo.

═══════════════════════════════════════════════════════════════
CONOCIMIENTO: ARCHIVO DE ERRORES DE VALIDACIÓN ARCA
═══════════════════════════════════════════════════════════════
Cuando el usuario sube TAMBIÉN el archivo de errores ARCA (el CSV que devuelve ARCA
después de rechazar una presentación), podés hacer un diagnóstico mucho más preciso.

El archivo de errores tiene este formato CSV:
  Cuil/Dato de referencia; Descripción
  CUIL: 20318798355; La base imponible 9 informada a nivel de nomina (650.305,28) \
        difiere de la determinada (644.400,35) a partir de las liquidaciones ingresadas.

El error más frecuente es "base imponible N informada ≠ determinada":
  - Base 1, 2, 3: diferencias en SIPA/FNE → revisar conceptos remunerativos
  - Base 4, 5: diferencias en OS/INSSJP → revisar tope MOPRE o concepto OS
  - Base 9 (Rem. neta de detracción, Ley 27.430): diferencia en la detracción aplicada
    → el error más sensible, relacionado con los bugs D, G, K del sistema

CUANDO HAY ARCHIVO DE ERRORES ARCA:
  1. Llamá primero parsear_errores_arca para entender el volumen y los tipos.
  2. Si también hay LSD disponible, llamá cruzar_errores_con_lsd para diagnosticar
     la causa raíz de cada error cruzando REG03 (conceptos) y REG04 (bases informadas).
  3. Explicá al consultor: qué empleado, qué base, cuánto difiere, y por qué.
  4. Indicá si la corrección requiere recalcular desde e-SUELDOS o si se puede
     corregir directamente en el TXT.

═══════════════════════════════════════════════════════════════
INSTRUCCIONES DE ANÁLISIS
═══════════════════════════════════════════════════════════════
1. Llamá info_archivo primero para entender el volumen.
2. Si hay archivo de errores ARCA: llamá parsear_errores_arca + cruzar_errores_con_lsd.
3. Ejecutá TODAS las validaciones de estructura antes de redactar el informe.
4. El informe final debe estar en español, con secciones claras y numeradas.
5. Clasificá cada problema como CRÍTICO (ARCA rechazará) o ADVERTENCIA (revisar).
6. Para cada problema: indicá causa, cantidad de CUILs afectados, y solución concreta.
7. Al final: veredicto claro "PRESENTABLE" o "SERÁ RECHAZADO" con fundamentación.
8. Si el archivo está limpio en todos los checks, indicalo explícitamente como una buena noticia.
9. Si te preguntan sobre la lógica interna del sistema (sin archivo TXT), respondé
   usando el conocimiento de arquitectura y cálculo documentado arriba.
"""

# ---------------------------------------------------------------------------
# Loop agentic con Gemini
# ---------------------------------------------------------------------------

def ejecutar_agente(ruta: str) -> None:
    """Orquesta el agente Gemini con function calling y PDFs de normativa."""
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("\nERROR: Variable de entorno GEMINI_API_KEY no configurada.")
        print("  Obtené tu key en: https://aistudio.google.com/apikey")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    # Ejecutar validaciones obligatorias antes del razonamiento del modelo
    resultados_prechecks = {}
    for tool_name in PRECHECKS:
        try:
            resultados_prechecks[tool_name] = TOOL_FUNCTIONS_ALL[tool_name](ruta)
        except Exception as e:
            resultados_prechecks[tool_name] = {"ok": False, "error": str(e)}


    # Cargar PDFs de normativa (si existen)
    pdf_refs = cargar_refs()
    pdf_parts = []
    if pdf_refs:
        print(f"  📚 Cargando {len(pdf_refs)} PDFs de normativa...")
        for ref in pdf_refs:
            try:
                pdf_parts.append(gtypes.Part.from_uri(
                    file_uri=ref["uri"],
                    mime_type="application/pdf"
                ))
            except Exception as e:
                print(f"  ⚠  No se pudo cargar {ref['display_name']}: {e}")

    # Mensaje inicial — incluir los PDFs como partes si existen
    msg_texto = (
        f"Analizá el siguiente archivo LSD y producí un informe completo de validación.\n\n"
        f"Archivo: {os.path.abspath(ruta)}\n\n"
        "Usá todas las herramientas disponibles. "
        "El informe debe incluir:\n"
        "  1. Resumen ejecutivo (3-5 líneas)\n"
        "  2. Problemas encontrados ordenados por severidad (CRÍTICO primero)\n"
        "  3. Para cada problema: causa, CUILs afectados (con ejemplos), solución\n"
        "  4. Veredicto final: ¿ARCA aceptará o rechazará este archivo?"
    )
    if pdf_parts:
        msg_texto += f"\n\nAdjunto {len(pdf_parts)} documentos de normativa LSD para que los uses como referencia."

    msg_texto += (
        "\n\nRESULTADOS DE VALIDACIONES OBLIGATORIAS:\n"
        + json.dumps(resultados_prechecks, ensure_ascii=False, indent=2)
    )

    # Construir contenido inicial: texto + PDFs opcionales
    contenido_inicial = gtypes.Content(
        role="user",
        parts=pdf_parts + [gtypes.Part.from_text(text=msg_texto)]
    )

    # Config del modelo
    herramientas = gtypes.Tool(function_declarations=TOOL_DECLARATIONS_ALL)
    config = gtypes.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[herramientas],
        temperature=0.1,
    )

    print()
    print("=" * 65)
    print("  AGENTE LSD — Validador ARCA/AFIP  [Gemini 3.5 Flash]")
    print("=" * 65)
    print(f"  Archivo : {os.path.abspath(ruta)}")
    print(f"  Modelo  : {MODELO}")
    print(f"  Normativa: {len(pdf_parts)} PDFs adjuntos" if pdf_parts else "  Normativa: solo conocimiento base")
    print()
    print("  Analizando...")
    print()

    # Historial de la conversación (Gemini usa lista de Content)
    historial: list[gtypes.Content] = [contenido_inicial]

    paso = 0
    while True:
        paso += 1
        if paso > 25:
            print("\n[AVISO] Límite de 25 iteraciones alcanzado.")
            break

        response = client.models.generate_content(
            model=MODELO,
            contents=historial,
            config=config,
        )

        # Agregar respuesta al historial
        historial.append(response.candidates[0].content)

        # ¿Terminó?
        if response.candidates[0].finish_reason.name in ("STOP", "MAX_TOKENS"):
            # Imprimir el texto final
            for part in response.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    print(part.text)
            break

        # Procesar function calls
        fn_calls = [
            part.function_call
            for part in response.candidates[0].content.parts
            if hasattr(part, "function_call") and part.function_call
        ]

        if not fn_calls:
            # Sin tool calls y sin STOP → imprimir texto parcial y salir
            for part in response.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    print(part.text)
            break

        # Ejecutar cada function call y construir las respuestas
        fn_responses = []
        for fc in fn_calls:
            fn_name  = fc.name
            fn_input = dict(fc.args)  # Struct → dict

            label = f"{fn_name}({', '.join(f'{k}={v!r}' for k, v in fn_input.items())})"
            print(f"  → {label}")

            if fn_name in TOOL_FUNCTIONS_ALL:
                try:
                    result = TOOL_FUNCTIONS_ALL[fn_name](**fn_input)
                except Exception as exc:
                    result = {"ok": False, "error": f"Excepción en {fn_name}: {exc}"}
            else:
                result = {"ok": False, "error": f"Herramienta desconocida: {fn_name}"}

            fn_responses.append(
                gtypes.Part.from_function_response(
                    name=fn_name,
                    response=result,
                )
            )

        # Agregar respuestas de las tools al historial
        historial.append(gtypes.Content(role="user", parts=fn_responses))

    print()
    print("=" * 65)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print(f"\nUso: python {sys.argv[0]} <archivo_lsd.txt>")
        sys.exit(1)

    archivo = sys.argv[1]
    if not os.path.exists(archivo):
        print(f"\nERROR: Archivo no encontrado: {archivo}")
        sys.exit(1)

    ejecutar_agente(archivo)