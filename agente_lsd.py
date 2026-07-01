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
    Opcional (base de conocimiento PDF):
    Correr primero: python knowledge_loader.py
"""

import sys
import os
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

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
REG03_CONCEPTO_START = 13; REG03_CONCEPTO_END = 23
REG03_CANTIDAD_START = 23; REG03_CANTIDAD_END = 28
REG03_UNIDAD_START = 28; REG03_UNIDAD_END = 29
REG03_IMP_START = 29; REG03_IMP_END = 44
REG03_DEB_CRED_START = 44; REG03_DEB_CRED_END = 45
REG03_PERIODO_AJUSTE_START = 45; REG03_PERIODO_AJUSTE_END = 51
REG04_BASE4_START  = 220; REG04_BASE4_END    = 235

# REG04 de 370 chars (export e-Sueldos): Rem10 en 340-355, importe detracción en 355-370
REG04_REM10_START = 340; REG04_REM10_END = 355
REG04_IMPORTE_DETRACCION_START = 355; REG04_IMPORTE_DETRACCION_END = 370
REG04_BASE10_START = REG04_REM10_START
REG04_BASE10_END = REG04_REM10_END
REG04_DETRACCION_START = REG04_IMPORTE_DETRACCION_START
REG04_DETRACCION_END = REG04_IMPORTE_DETRACCION_END

# Nuevas constantes — spec LSD v2 completa (posiciones 0-indexed)
REG01_PERIODO_START       = 15;  REG01_PERIODO_END       = 21
REG01_TIPO_LIQ_START      = 21;  REG01_TIPO_LIQ_END      = 22
REG01_NRO_LIQ_START       = 22;  REG01_NRO_LIQ_END       = 27
REG01_DIAS_BASE_START     = 27;  REG01_DIAS_BASE_END     = 29
REG01_CANT_REG04_START    = 29;  REG01_CANT_REG04_END    = 35
REG02_CBU_START           = 73;  REG02_CBU_END           = 95
REG02_DIAS_LIQ_START      = 95;  REG02_DIAS_LIQ_END      = 98
REG02_FECHA_PAGO_START    = 98;  REG02_FECHA_PAGO_END    = 106
REG02_FECHA_RUB_START     = 106; REG02_FECHA_RUB_END     = 114
REG02_FORMA_PAGO_START    = 114; REG02_FORMA_PAGO_END    = 115
REG04_REM_BRUTA_START     = 160; REG04_REM_BRUTA_END     = 175
REG04_BASE1_START         = 175; REG04_BASE1_END         = 190
REG04_BASE2_START         = 190; REG04_BASE2_END         = 205
REG04_BASE3_START         = 205; REG04_BASE3_END         = 220
REG04_BASE5_START         = 235; REG04_BASE5_END         = 250
REG04_BASE6_START         = 250; REG04_BASE6_END         = 265
REG04_BASE7_START         = 265; REG04_BASE7_END         = 280
REG04_BASE8_START         = 280; REG04_BASE8_END         = 295
REG04_BASE9_START         = 295; REG04_BASE9_END         = 310
REG04_DIF_APORTE_SS_START = 310; REG04_DIF_APORTE_SS_END = 325
REG04_DIF_CONTR_SS_START  = 325; REG04_DIF_CONTR_SS_END  = 340
REG04_HORAS_EXTRAS_START  = REG04_REM10_START
REG04_HORAS_EXTRAS_END    = REG04_REM10_END

# Posiciones REG04 bases imponibles (0-indexed, fin exclusivo) — compartidas con validador_errores
REG04_BASE_POSICIONES = {
    1:  (REG04_BASE1_START, REG04_BASE1_END),
    2:  (REG04_BASE2_START, REG04_BASE2_END),
    3:  (REG04_BASE3_START, REG04_BASE3_END),
    4:  (REG04_BASE4_START, REG04_BASE4_END),
    5:  (REG04_BASE5_START, REG04_BASE5_END),
    6:  (REG04_BASE6_START, REG04_BASE6_END),
    7:  (REG04_BASE7_START, REG04_BASE7_END),
    8:  (REG04_BASE8_START, REG04_BASE8_END),
    9:  (REG04_BASE9_START, REG04_BASE9_END),
    10: (REG04_REM10_START, REG04_REM10_END),
}

# Longitudes exactas por tipo (ancho fijo)
LONGITUDES_REQUERIDAS = {
    '01': 35,
    '02': 115,
    '03': 51,   # sin el sufijo ".e-s" de e-SUELDOS (aceptable hasta 55)
    '04': 370,
    '05': 65,   # trabajadores eventuales; puede no existir
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
# PARSER FORMAL + ÍNDICES + MOTOR DE REGLAS DETERMINÍSTICO
# ---------------------------------------------------------------------------

_ANALISIS_CACHE: dict[tuple[str, float, int], dict] = {}

RULE_CATALOG = {
    "LSD-REG01-STRUCT-001": {
        "severidad": "CRITICO",
        "campo": "REG01",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "El archivo debe tener exactamente un REG01 y debe ser la primera línea no vacía.",
        "fix_hint": "Regenerar el TXT desde e-Sueldos para reconstruir la cabecera.",
    },
    "LSD-REG02-STRUCT-001": {
        "severidad": "CRITICO",
        "campo": "REG02",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "El archivo debe incluir registros REG02 de empleados.",
        "fix_hint": "Revisar la liquidación y volver a exportar el LSD.",
    },
    "LSD-CUIL-ORPHAN-001": {
        "severidad": "CRITICO",
        "campo": "CUIL",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "Todo CUIL informado en REG03, REG04 o REG05 debe tener cabecera REG02.",
        "fix_hint": "Regenerar el archivo para que cada trabajador tenga su registro de datos generales.",
    },
    "LSD-TIPO-UNKNOWN-001": {
        "severidad": "ADVERTENCIA",
        "campo": "Tipo de registro",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "El archivo contiene tipos de registro fuera de 01, 02, 03, 04 y 05.",
        "fix_hint": "Verificar que el TXT no tenga líneas extra o contenido agregado manualmente.",
    },
    "LSD-REG01-COUNT04-001": {
        "severidad": "CRITICO",
        "campo": "REG01 cantidad REG04",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "La cantidad de REG04 declarada en REG01 debe coincidir con los REG04 reales.",
        "fix_hint": "Regenerar el TXT desde e-Sueldos para corregir el contador.",
    },
    "LSD-LENGTH-001": {
        "severidad": "CRITICO",
        "campo": "Longitud de registros",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "Los registros deben respetar el ancho fijo del layout LSD.",
        "fix_hint": "No editar el TXT manualmente; regenerarlo desde e-Sueldos.",
    },
    "LSD-REG04-DUP-001": {
        "severidad": "CRITICO",
        "campo": "REG04",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Un CUIL tiene más de un REG04 y puede duplicar bases imponibles.",
        "fix_hint": "Consolidar la liquidación del empleado y regenerar el LSD.",
    },
    "LSD-NUM-COMMA-001": {
        "severidad": "CRITICO",
        "campo": "Campos numéricos",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Los campos numéricos no deben contener comas.",
        "fix_hint": "Corregir el origen del formato numérico y regenerar el TXT.",
    },
    "LSD-NUM-NEG-001": {
        "severidad": "CRITICO",
        "campo": "Importes",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "El LSD no admite importes negativos en campos monetarios.",
        "fix_hint": "Informar descuentos con el indicador correspondiente y no como importe negativo.",
    },
    "LSD-NUM-SCI-001": {
        "severidad": "CRITICO",
        "campo": "Campos numéricos",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Los campos numéricos no deben usar notación científica.",
        "fix_hint": "Actualizar e-Sueldos si corresponde y regenerar el archivo.",
    },
    "LSD-NUM-FORMAT-001": {
        "severidad": "CRITICO",
        "campo": "Campos numéricos",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "Los campos numéricos de ancho fijo deben tener solo dígitos y longitud exacta.",
        "fix_hint": "Regenerar el TXT sin editar importes manualmente ni usar separadores.",
    },
    "LSD-REG03-DEB-CRED-001": {
        "severidad": "CRITICO",
        "campo": "REG03 débito/crédito",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "El indicador de débito/crédito de REG03 debe ser D o C.",
        "fix_hint": "Corregir la naturaleza del concepto en e-Sueldos y regenerar el TXT.",
    },
    "LSD-REG03-AJUSTE-001": {
        "severidad": "CRITICO",
        "campo": "REG03 período ajuste",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "El período de ajuste de REG03 debe ser AAAAMM, 000000 o blanco.",
        "fix_hint": "Revisar conceptos retroactivos y regenerar el TXT.",
    },
    "LSD-REG02-CBU-001": {
        "severidad": "CRITICO",
        "campo": "REG02 CBU",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "Si forma de pago es 3, el CBU debe tener exactamente 22 dígitos en REG02 posiciones 74-95.",
        "fix_hint": "Completar un CBU válido o cambiar la forma de pago y regenerar el TXT.",
    },
    "LSD-REG01-PERIOD-001": {
        "severidad": "CRITICO",
        "campo": "REG01 período",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "El período de REG01 debe tener formato AAAAMM válido.",
        "fix_hint": "Revisar período, tipo de envío y número de presentación antes de exportar.",
    },
    "LSD-REG01-CUIT-001": {
        "severidad": "CRITICO",
        "campo": "REG01 CUIT empleador",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "El CUIT empleador de REG01 debe tener 11 dígitos y dígito verificador válido.",
        "fix_hint": "Revisar el CUIT de la empresa configurado en e-Sueldos y regenerar el TXT.",
    },
    "LSD-REG01-LIQ-001": {
        "severidad": "CRITICO",
        "campo": "REG01 tipo/número de liquidación",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "REG01 debe respetar tipo de liquidación, número de liquidación y días base según el tipo de envío.",
        "fix_hint": "Revisar el período y la liquidación exportada. Para SJ usar tipo M/Q/D/H y días base 30.",
    },
    "LSD-CUIL-FORMAT-001": {
        "severidad": "CRITICO",
        "campo": "CUIL",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "Los CUIL informados deben tener 11 dígitos y dígito verificador válido.",
        "fix_hint": "Corregir el CUIL del empleado en e-Sueldos y volver a exportar.",
    },
    "LSD-REG02-DUP-001": {
        "severidad": "CRITICO",
        "campo": "REG02",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "No debe haber más de un REG02 para el mismo CUIL.",
        "fix_hint": "Revisar legajos duplicados o relaciones repetidas del trabajador y regenerar el TXT.",
    },
    "LSD-EMP-INTEGRITY-001": {
        "severidad": "CRITICO",
        "campo": "Integridad por empleado",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "Si un empleado tiene conceptos REG03, debe tener bases/atributos REG04.",
        "fix_hint": "Recalcular el Libro Sueldo Digital para que se generen los atributos del empleado.",
    },
    "LSD-REG02-FECHA-001": {
        "severidad": "CRITICO",
        "campo": "REG02 fechas",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "La fecha de pago debe tener formato AAAAMMDD válido; fecha de rúbrica es opcional pero si se informa debe ser válida.",
        "fix_hint": "Corregir fecha de pago/rúbrica en la liquidación y regenerar el TXT.",
    },
    "LSD-REG02-FORMA-PAGO-001": {
        "severidad": "CRITICO",
        "campo": "REG02 forma de pago",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "La forma de pago debe ser 1, 2 o 3.",
        "fix_hint": "Seleccionar una forma de pago válida para el empleado.",
    },
    "LSD-REG04-BASE-001": {
        "severidad": "ADVERTENCIA",
        "campo": "REG04 bases imponibles",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Base 4 en cero con Base 10 con valor puede indicar parametrización incorrecta 560/570.",
        "fix_hint": "Revisar conceptos no contributivos y parametrización de Guía 45.",
    },
    "LSD-REG04-REM-001": {
        "severidad": "CRITICO",
        "campo": "REG04 Remuneración/Base 1",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "La Base 1 no debe superar la remuneración bruta.",
        "fix_hint": "Recalcular el LSD desde e-Sueldos.",
    },
    "LSD-REG04-BASE-NEG-001": {
        "severidad": "CRITICO",
        "campo": "REG04 bases imponibles",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "Las bases imponibles y diferenciales no deben ser negativas.",
        "fix_hint": "Revisar importes, detracción y bases calculadas antes de exportar.",
    },
    "LSD-REG04-DETRACCION-001": {
        "severidad": "CRITICO",
        "campo": "REG04 detracción/base 10",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "Base imponible 10 debe ser coherente con Base 2 menos importe a detraer.",
        "fix_hint": "Recalcular detracción Ley 27.430 y regenerar el LSD.",
    },
    "LSD-REG04-BASES-CONCEPTOS-001": {
        "severidad": "CRITICO",
        "campo": "REG03/REG04 bases imponibles",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Las bases imponibles informadas en REG04 no son coherentes con conceptos REG03 conocidos por generar diferencias ARCA.",
        "fix_hint": "Revisar parametrización de conceptos no remunerativos/detracción y recalcular el LSD desde e-Sueldos.",
    },
    "LSD-REG03-CONCEPTO-001": {
        "severidad": "ADVERTENCIA",
        "campo": "REG03 concepto ARCA",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Concepto ARCA obsoleto o de uso problemático detectado.",
        "fix_hint": "Revisar equivalencias de conceptos según Guía 45.",
    },
    "LSD-REG03-SAC-001": {
        "severidad": "CRITICO",
        "campo": "REG03 SAC",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Conceptos SAC semestrales solo corresponden en junio o diciembre.",
        "fix_hint": "Usar el concepto proporcional/correcto o mover la liquidación al período correspondiente.",
    },
    "LSD-REG04-BASE4-BASE5-001": {
        "severidad": "CRITICO",
        "campo": "REG04 bases OS/INSSJP",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Base 4 (Obra Social) y Base 5 (INSSJP) deben coincidir en el REG04.",
        "fix_hint": "Recalcular la liquidación y regenerar el LSD desde e-Sueldos.",
    },
    "LSD-REG04-BASE9-001": {
        "severidad": "ADVERTENCIA",
        "campo": "REG04 Base 9",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Base 9 supera Base 1 sin conceptos que justifiquen el incremento.",
        "fix_hint": "Revisar conceptos no remunerativos, detracción y parametrización de bases antes de exportar.",
    },
    "LSD-REG04-BASE9-002": {
        "severidad": "CRITICO",
        "campo": "REG04 Base 9",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Base 9 no puede superar Base 2.",
        "fix_hint": "Recalcular bases imponibles y regenerar el LSD.",
    },
    "LSD-REG04-BASE9-003": {
        "severidad": "CRITICO",
        "campo": "REG04 Base 9",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Base 9 informa el total sin tope mientras Base 1/4 están topadas (MOPRE).",
        "fix_hint": "Recalcular detracción/topes y regenerar el REG04 desde e-Sueldos.",
    },
    "LSD-REG04-BASE2-001": {
        "severidad": "ADVERTENCIA",
        "campo": "REG04 Base 2",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "Base 2 en cero con Base 4 positiva indica REG04 inconsistente.",
        "fix_hint": "Regenerar la liquidación completa del empleado y volver a exportar el LSD.",
    },
    "LSD-REG04-REM10-001": {
        "severidad": "CRITICO",
        "campo": "REG04 Rem10 / detracción",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "Sin importe de detracción, Rem10 (Base imponible 10) debe ser cero.",
        "fix_hint": "Recalcular detracción Ley 27.430 en e-Sueldos y regenerar el LSD.",
    },
    "LSD-REG04-DETRACCION-MAX": {
        "severidad": "CRITICO",
        "campo": "REG04 importe a detraer",
        "fuente_pdf": "validaciones.pdf",
        "mensaje": "El importe a detraer supera el tope legal máximo establecido (7038,70).",
        "fix_hint": "Ajustar el importe de la detracción para que no supere el tope de la Ley 27.430.",
    },
    "LSD-REG04-TOPE-MOPRE-SAC": {
        "severidad": "CRITICO",
        "campo": "REG04 Bases Imponibles",
        "fuente_pdf": "LS_Conceptos_Basicos_y_Guia_de_Uso_V2.0.pdf",
        "mensaje": "La Base 1 es rechazada por ARCA porque el concepto SAC tiene muy pocos días informados (tope proporcional estricto).",
        "fix_hint": "Cambiar el concepto a SAC semestral o informar los días reales del semestre en las unidades.",
    },
}

def _slice(linea: str, start: int, end: int) -> str:
    return linea[start:end] if len(linea) > start else ''

def _int_or_none(valor: str) -> int | None:
    valor = (valor or '').strip()
    if not valor or not re.fullmatch(r'-?\d+', valor):
        return None
    return int(valor)

def _decimal_centavos_or_none(valor: str) -> Decimal | None:
    valor = (valor or '').strip()
    if not re.fullmatch(r'-?\d+', valor):
        return None
    try:
        return Decimal(valor) / Decimal(100)
    except InvalidOperation:
        return None

def _campo_numerico_exacto(valor: str, longitud: int, permitir_blanco: bool = False) -> bool:
    if permitir_blanco and not (valor or '').strip():
        return True
    return len(valor) == longitud and valor.isdigit()

def _money_or_none(valor: str) -> Decimal | None:
    return _decimal_centavos_or_none(valor)

def _periodo_yyyymm_valido(valor: str, permitir_blanco: bool = False, permitir_ceros: bool = False) -> bool:
    valor = valor or ''
    if permitir_blanco and not valor.strip():
        return True
    if permitir_ceros and valor == "000000":
        return True
    if not re.fullmatch(r'\d{6}', valor):
        return False
    return 1 <= int(valor[4:6]) <= 12

def _fecha_yyyymmdd_valida(valor: str, permitir_blanco: bool = False) -> bool:
    valor = valor or ''
    if permitir_blanco and not valor.strip():
        return True
    if valor == "00000000":
        return True
    if not re.fullmatch(r'\d{8}', valor):
        return False
    anio = int(valor[:4]); mes = int(valor[4:6]); dia = int(valor[6:8])
    if not (1900 <= anio <= 2100 and 1 <= mes <= 12):
        return False
    dias_mes = [31, 29 if (anio % 400 == 0 or (anio % 4 == 0 and anio % 100 != 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return 1 <= dia <= dias_mes[mes - 1]

def _cuit_cuil_valido(valor: str) -> bool:
    if not re.fullmatch(r'\d{11}', valor or ''):
        return False
    pesos = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    suma = sum(int(valor[i]) * pesos[i] for i in range(10))
    resto = suma % 11
    digito = 11 - resto
    if digito == 11:
        digito = 0
    elif digito == 10:
        digito = 9
    return digito == int(valor[-1])

def parse_reg01(linea: str, nro_linea: int) -> dict:
    return {
        "tipo": "01", "linea": nro_linea, "raw": linea, "longitud": len(linea),
        "cuit_empleador": _slice(linea, 2, 13),
        "tipo_envio": _slice(linea, 13, 15),
        "periodo": _slice(linea, REG01_PERIODO_START, REG01_PERIODO_END),
        "tipo_liquidacion": _slice(linea, 21, 22),
        "nro_presentacion": _slice(linea, 22, 27),
        "dias_base": _slice(linea, 27, 29),
        "cantidad_reg04": _int_or_none(_slice(linea, REG01_CANT_REG04_START, REG01_CANT_REG04_END)),
    }

def parse_reg02(linea: str, nro_linea: int) -> dict:
    return {
        "tipo": "02", "linea": nro_linea, "raw": linea, "longitud": len(linea),
        "cuil": _slice(linea, 2, 13),
        "legajo": _slice(linea, 13, 23),
        "dependencia": _slice(linea, 23, 73),
        "cbu": _slice(linea, REG02_CBU_START, REG02_CBU_END),
        "dias_liquidados": _slice(linea, 95, 98),
        "fecha_pago": _slice(linea, 98, 106),
        "fecha_rubrica": _slice(linea, 106, 114),
        "forma_pago": _slice(linea, REG02_FORMA_PAGO_START, REG02_FORMA_PAGO_END),
    }

def parse_reg03(linea: str, nro_linea: int) -> dict:
    return {
        "tipo": "03", "linea": nro_linea, "raw": linea, "longitud": len(linea),
        "cuil": _slice(linea, 2, 13),
        "codigo_concepto": _slice(linea, REG03_CONCEPTO_START, REG03_CONCEPTO_END).strip(),
        "codigo_arca": _slice(linea, REG03_COD_START, REG03_COD_END).strip(),
        "cantidad": _slice(linea, REG03_CANTIDAD_START, REG03_CANTIDAD_END).strip(),
        "unidad": _slice(linea, REG03_UNIDAD_START, REG03_UNIDAD_END).strip(),
        "importe_raw": _slice(linea, REG03_IMP_START, REG03_IMP_END).strip(),
        "importe": _money_or_none(_slice(linea, REG03_IMP_START, REG03_IMP_END)),
        "debito_credito": _slice(linea, REG03_DEB_CRED_START, REG03_DEB_CRED_END).strip(),
        "periodo_ajuste": _slice(linea, REG03_PERIODO_AJUSTE_START, REG03_PERIODO_AJUSTE_END).strip(),
    }

def parse_reg04(linea: str, nro_linea: int) -> dict:
    return {
        "tipo": "04", "linea": nro_linea, "raw": linea, "longitud": len(linea),
        "cuil": _slice(linea, 2, 13),
        "rem_bruta_raw": _slice(linea, REG04_REM_BRUTA_START, REG04_REM_BRUTA_END).strip(),
        "rem_bruta": _money_or_none(_slice(linea, REG04_REM_BRUTA_START, REG04_REM_BRUTA_END)),
        "base1_raw": _slice(linea, REG04_BASE1_START, REG04_BASE1_END).strip(),
        "base1": _money_or_none(_slice(linea, REG04_BASE1_START, REG04_BASE1_END)),
        "base2_raw": _slice(linea, REG04_BASE2_START, REG04_BASE2_END).strip(),
        "base2": _money_or_none(_slice(linea, REG04_BASE2_START, REG04_BASE2_END)),
        "base3_raw": _slice(linea, REG04_BASE3_START, REG04_BASE3_END).strip(),
        "base3": _money_or_none(_slice(linea, REG04_BASE3_START, REG04_BASE3_END)),
        "base4_raw": _slice(linea, REG04_BASE4_START, REG04_BASE4_END).strip(),
        "base4": _money_or_none(_slice(linea, REG04_BASE4_START, REG04_BASE4_END)),
        "base5_raw": _slice(linea, REG04_BASE5_START, REG04_BASE5_END).strip(),
        "base5": _money_or_none(_slice(linea, REG04_BASE5_START, REG04_BASE5_END)),
        "base6_raw": _slice(linea, REG04_BASE6_START, REG04_BASE6_END).strip(),
        "base6": _money_or_none(_slice(linea, REG04_BASE6_START, REG04_BASE6_END)),
        "base7_raw": _slice(linea, REG04_BASE7_START, REG04_BASE7_END).strip(),
        "base7": _money_or_none(_slice(linea, REG04_BASE7_START, REG04_BASE7_END)),
        "base8_raw": _slice(linea, REG04_BASE8_START, REG04_BASE8_END).strip(),
        "base8": _money_or_none(_slice(linea, REG04_BASE8_START, REG04_BASE8_END)),
        "base9_raw": _slice(linea, REG04_BASE9_START, REG04_BASE9_END).strip(),
        "base9": _money_or_none(_slice(linea, REG04_BASE9_START, REG04_BASE9_END)),
        "dif_aporte_ss_raw": _slice(linea, REG04_DIF_APORTE_SS_START, REG04_DIF_APORTE_SS_END).strip(),
        "dif_aporte_ss": _money_or_none(_slice(linea, REG04_DIF_APORTE_SS_START, REG04_DIF_APORTE_SS_END)),
        "dif_contr_ss_raw": _slice(linea, REG04_DIF_CONTR_SS_START, REG04_DIF_CONTR_SS_END).strip(),
        "dif_contr_ss": _money_or_none(_slice(linea, REG04_DIF_CONTR_SS_START, REG04_DIF_CONTR_SS_END)),
        "base10_raw": _slice(linea, REG04_BASE10_START, REG04_BASE10_END).strip(),
        "base10": _money_or_none(_slice(linea, REG04_BASE10_START, REG04_BASE10_END)),
        "importe_detraer_raw": _slice(linea, REG04_DETRACCION_START, REG04_DETRACCION_END).strip(),
        "importe_detraer": _money_or_none(_slice(linea, REG04_DETRACCION_START, REG04_DETRACCION_END)),
    }

def parse_reg05(linea: str, nro_linea: int) -> dict:
    return {
        "tipo": "05", "linea": nro_linea, "raw": linea, "longitud": len(linea),
        "cuil": _slice(linea, 2, 13),
        "categoria_profesional": _slice(linea, 13, 19),
        "puesto": _slice(linea, 19, 23),
        "fecha_ingreso": _slice(linea, 23, 31),
        "fecha_egreso": _slice(linea, 31, 39),
        "importe_raw": _slice(linea, 39, 54).strip(),
        "importe": _money_or_none(_slice(linea, 39, 54)),
        "cuit_eventual": _slice(linea, 54, 65),
    }

def parse_registro(linea: str, nro_linea: int) -> dict:
    tipo = _tipo(linea)
    if tipo == '01': return parse_reg01(linea, nro_linea)
    if tipo == '02': return parse_reg02(linea, nro_linea)
    if tipo == '03': return parse_reg03(linea, nro_linea)
    if tipo == '04': return parse_reg04(linea, nro_linea)
    if tipo == '05': return parse_reg05(linea, nro_linea)
    return {"tipo": tipo, "linea": nro_linea, "raw": linea, "longitud": len(linea), "cuil": _cuil(linea)}

def _add_issue(issues: list[dict], rule_id: str, **extra) -> None:
    rule = RULE_CATALOG[rule_id]
    issues.append({"id": rule_id, **rule, **extra})

def _add_numeric_issue(issues: list[dict], reg: dict, campo: str, valor: str, longitud: int, permitir_blanco: bool = False) -> None:
    if _campo_numerico_exacto(valor, longitud, permitir_blanco=permitir_blanco):
        return
    _add_issue(
        issues,
        "LSD-NUM-FORMAT-001",
        linea=reg["linea"],
        cuil=reg.get("cuil"),
        detalle={
            "tipo": reg["tipo"],
            "campo": campo,
            "valor": valor,
            "longitud": len(valor),
            "longitud_esperada": longitud,
            "permite_blanco": permitir_blanco,
        },
    )

def _sumar_conceptos(conceptos: list[dict], codigos: set[str]) -> Decimal:
    total = Decimal("0")
    for concepto in conceptos:
        if concepto.get("codigo_concepto", "").strip() not in codigos:
            continue
        importe = concepto.get("importe")
        if importe is None:
            continue
        if concepto.get("debito_credito") == "D":
            total -= importe
        else:
            total += importe
    return total

def _raws_conceptos(conceptos: list[dict], codigos: set[str], limite: int = 20) -> list[dict]:
    relacionados = []
    for concepto in conceptos:
        if concepto.get("codigo_concepto", "").strip() not in codigos:
            continue
        relacionados.append({
            "linea": concepto.get("linea"),
            "codigo": concepto.get("codigo_concepto", ""),
            "importe": concepto.get("importe_raw", ""),
            "debito_credito": concepto.get("debito_credito", ""),
            "raw": concepto.get("raw", ""),
        })
        if len(relacionados) >= limite:
            break
    return relacionados

def _construir_analisis(ruta: str) -> dict:
    lineas = _leer_lineas(ruta)
    registros = []
    by_type: dict[str, list[dict]] = defaultdict(list)
    empleados: dict[str, dict] = {}
    conceptos_por_cuil: dict[str, list[dict]] = defaultdict(list)
    bases_por_cuil: dict[str, list[dict]] = defaultdict(list)
    eventuales_por_cuil: dict[str, list[dict]] = defaultdict(list)

    for nro, linea in enumerate(lineas, 1):
        if not linea.strip():
            continue
        reg = parse_registro(linea, nro)
        registros.append(reg)
        by_type[reg["tipo"]].append(reg)
        cuil = reg.get("cuil")
        if reg["tipo"] == "02" and cuil:
            empleados[cuil] = reg
        elif reg["tipo"] == "03" and cuil:
            conceptos_por_cuil[cuil].append(reg)
        elif reg["tipo"] == "04" and cuil:
            bases_por_cuil[cuil].append(reg)
        elif reg["tipo"] == "05" and cuil:
            eventuales_por_cuil[cuil].append(reg)

    analisis = {
        "ruta": os.path.abspath(ruta),
        "size_bytes": os.path.getsize(ruta),
        "total_lineas": len(lineas),
        "lineas_vacias": sum(1 for l in lineas if not l.strip()),
        "registros": registros,
        "by_type": dict(by_type),
        "empleados": empleados,
        "conceptos_por_cuil": dict(conceptos_por_cuil),
        "bases_por_cuil": dict(bases_por_cuil),
        "eventuales_por_cuil": dict(eventuales_por_cuil),
        "registros_por_tipo": {t: len(v) for t, v in by_type.items()},
        "issues": [],
    }
    analisis["issues"] = ejecutar_reglas_deterministicas(analisis)
    return analisis

def obtener_analisis_lsd(ruta: str) -> dict:
    abs_path = os.path.abspath(ruta)
    stat = os.stat(abs_path)
    key = (abs_path, stat.st_mtime, stat.st_size)
    if key not in _ANALISIS_CACHE:
        _ANALISIS_CACHE.clear()
        _ANALISIS_CACHE[key] = _construir_analisis(abs_path)
    return _ANALISIS_CACHE[key]

def _issues(analisis: dict, rule_id: str | None = None) -> list[dict]:
    if rule_id is None:
        return analisis.get("issues", [])
    return [i for i in analisis.get("issues", []) if i.get("id") == rule_id]

def ejecutar_reglas_deterministicas(analisis: dict) -> list[dict]:
    issues: list[dict] = []
    by_type = analisis["by_type"]
    registros = analisis["registros"]
    reg01s = by_type.get("01", [])
    reg02s = by_type.get("02", [])

    if len(reg01s) != 1:
        _add_issue(issues, "LSD-REG01-STRUCT-001", detalle={"cantidad_reg01": len(reg01s)})
    elif registros and registros[0]["tipo"] != "01":
        _add_issue(issues, "LSD-REG01-STRUCT-001", detalle={"linea_reg01": reg01s[0]["linea"], "problema": "REG01 no es la primera línea"})

    if not reg02s:
        _add_issue(issues, "LSD-REG02-STRUCT-001", detalle={"cantidad_reg02": 0})

    cuils_02 = set(analisis["empleados"])
    for tipo, index_name in (("03", "conceptos_por_cuil"), ("04", "bases_por_cuil"), ("05", "eventuales_por_cuil")):
        huerfanos = sorted(set(analisis[index_name]) - cuils_02)
        if huerfanos:
            _add_issue(issues, "LSD-CUIL-ORPHAN-001", detalle={"tipo": tipo, "cantidad": len(huerfanos), "cuils": huerfanos[:20]})

    desconocidos = sorted(set(by_type) - {"01", "02", "03", "04", "05"})
    if desconocidos:
        _add_issue(issues, "LSD-TIPO-UNKNOWN-001", detalle={"tipos": desconocidos})

    if len(reg01s) == 1:
        declarado = reg01s[0].get("cantidad_reg04")
        real = len(by_type.get("04", []))
        if declarado is None or declarado != real:
            _add_issue(issues, "LSD-REG01-COUNT04-001", detalle={"declarado": declarado, "real": real})
        periodo = reg01s[0].get("periodo", "")
        tipo_envio = reg01s[0].get("tipo_envio", "")
        nro_pres = reg01s[0].get("nro_presentacion", "")
        errores = []
        if not re.fullmatch(r'\d{6}', periodo or ''):
            errores.append("periodo_no_numerico")
        else:
            mes = int(periodo[4:6])
            if not 1 <= mes <= 12:
                errores.append("mes_invalido")
        if tipo_envio not in ("SJ", "RE"):
            errores.append("tipo_envio_invalido")
        if not re.fullmatch(r'\d{5}', nro_pres or ''):
            errores.append("nro_presentacion_invalido")
        if errores:
            _add_issue(issues, "LSD-REG01-PERIOD-001", detalle={"errores": errores, "periodo": periodo, "tipo_envio": tipo_envio, "nro_presentacion": nro_pres})
        cuit_emp = reg01s[0].get("cuit_empleador", "")
        if not _cuit_cuil_valido(cuit_emp):
            _add_issue(issues, "LSD-REG01-CUIT-001", detalle={"cuit_empleador": cuit_emp})
        tipo_liq = reg01s[0].get("tipo_liquidacion", "")
        dias_base = reg01s[0].get("dias_base", "")
        nro_liq = reg01s[0].get("nro_presentacion", "")
        errores_liq = []
        if tipo_envio == "SJ":
            if tipo_liq not in ("M", "Q", "D", "H"):
                errores_liq.append("tipo_liquidacion_invalido_para_sj")
            if dias_base != "30":
                errores_liq.append("dias_base_debe_ser_30")
        elif tipo_envio == "RE":
            if tipo_liq.strip():
                errores_liq.append("tipo_liquidacion_debe_ir_en_blanco_para_re")
            if dias_base.strip():
                errores_liq.append("dias_base_debe_ir_en_blanco_para_re")
        if not re.fullmatch(r'\d{5}', nro_liq or ''):
            errores_liq.append("numero_liquidacion_no_numerico")
        if errores_liq:
            _add_issue(issues, "LSD-REG01-LIQ-001", detalle={"errores": errores_liq, "tipo_envio": tipo_envio, "tipo_liquidacion": tipo_liq, "dias_base": dias_base, "nro_liquidacion": nro_liq})

    reg02_por_cuil: dict[str, list[dict]] = defaultdict(list)
    for reg in reg02s:
        reg02_por_cuil[reg.get("cuil", "")].append(reg)
    for cuil, regs in reg02_por_cuil.items():
        if len(regs) > 1:
            _add_issue(issues, "LSD-REG02-DUP-001", cuil=cuil, detalle={"cantidad_reg02": len(regs), "lineas": [r["linea"] for r in regs]})
    for reg in reg02s:
        cuil = reg.get("cuil", "")
        if not _cuit_cuil_valido(cuil):
            _add_issue(issues, "LSD-CUIL-FORMAT-001", linea=reg["linea"], cuil=cuil, detalle={"tipo": "02"})
        forma_pago = reg.get("forma_pago", "")
        if forma_pago not in ("1", "2", "3"):
            _add_issue(issues, "LSD-REG02-FORMA-PAGO-001", linea=reg["linea"], cuil=cuil, detalle={"forma_pago": forma_pago})
        fecha_pago = reg.get("fecha_pago", "")
        fecha_rubrica = reg.get("fecha_rubrica", "")
        errores_fecha = []
        if not _fecha_yyyymmdd_valida(fecha_pago):
            errores_fecha.append("fecha_pago_invalida")
        if not _fecha_yyyymmdd_valida(fecha_rubrica, permitir_blanco=True):
            errores_fecha.append("fecha_rubrica_invalida")
        if errores_fecha:
            _add_issue(issues, "LSD-REG02-FECHA-001", linea=reg["linea"], cuil=cuil, detalle={"errores": errores_fecha, "fecha_pago": fecha_pago, "fecha_rubrica": fecha_rubrica})

    for tipo, index_name in (("03", "conceptos_por_cuil"), ("04", "bases_por_cuil"), ("05", "eventuales_por_cuil")):
        for cuil, regs in analisis[index_name].items():
            if cuil and not _cuit_cuil_valido(cuil):
                _add_issue(issues, "LSD-CUIL-FORMAT-001", linea=regs[0]["linea"], cuil=cuil, detalle={"tipo": tipo})

    for cuil, conceptos in analisis["conceptos_por_cuil"].items():
        if conceptos and not analisis["bases_por_cuil"].get(cuil):
            _add_issue(issues, "LSD-EMP-INTEGRITY-001", cuil=cuil, detalle={"conceptos": len(conceptos), "bases": 0, "lineas_conceptos": [r["linea"] for r in conceptos[:20]]})

    for reg in registros:
        req = LONGITUDES_REQUERIDAS.get(reg["tipo"])
        if not req:
            continue
        lon = reg["longitud"]
        if reg["tipo"] in ("03", "04"):
            if lon < req:
                _add_issue(issues, "LSD-LENGTH-001", linea=reg["linea"], cuil=reg.get("cuil"), detalle={"tipo": reg["tipo"], "longitud": lon, "minima": req})
        elif lon != req:
            _add_issue(issues, "LSD-LENGTH-001", linea=reg["linea"], cuil=reg.get("cuil"), detalle={"tipo": reg["tipo"], "longitud": lon, "requerida": req})

    for reg in reg01s:
        raw = reg["raw"]
        _add_numeric_issue(issues, reg, "cuit_empleador", _slice(raw, 2, 13), 11)
        _add_numeric_issue(issues, reg, "periodo", _slice(raw, REG01_PERIODO_START, REG01_PERIODO_END), 6)
        _add_numeric_issue(issues, reg, "nro_presentacion", _slice(raw, 22, 27), 5)
        _add_numeric_issue(issues, reg, "cantidad_reg04", _slice(raw, REG01_CANT_REG04_START, REG01_CANT_REG04_END), 6)
        if reg.get("tipo_envio") == "SJ":
            _add_numeric_issue(issues, reg, "dias_base", _slice(raw, 27, 29), 2)
        elif reg.get("tipo_envio") == "RE":
            _add_numeric_issue(issues, reg, "dias_base", _slice(raw, 27, 29), 2, permitir_blanco=True)

    for reg in reg02s:
        raw = reg["raw"]
        _add_numeric_issue(issues, reg, "cuil", _slice(raw, 2, 13), 11)
        _add_numeric_issue(issues, reg, "dias_liquidados", _slice(raw, 95, 98), 3)
        _add_numeric_issue(issues, reg, "fecha_pago", _slice(raw, 98, 106), 8)
        _add_numeric_issue(issues, reg, "fecha_rubrica", _slice(raw, 106, 114), 8, permitir_blanco=True)

    for reg in by_type.get("03", []):
        raw = reg["raw"]
        _add_numeric_issue(issues, reg, "cantidad", _slice(raw, REG03_CANTIDAD_START, REG03_CANTIDAD_END), 5)
        _add_numeric_issue(issues, reg, "importe", _slice(raw, REG03_IMP_START, REG03_IMP_END), 15)
        debito_credito = reg.get("debito_credito", "")
        if debito_credito not in ("D", "C"):
            _add_issue(issues, "LSD-REG03-DEB-CRED-001", linea=reg["linea"], cuil=reg.get("cuil"), detalle={"valor": debito_credito})
        periodo_ajuste = _slice(raw, REG03_PERIODO_AJUSTE_START, REG03_PERIODO_AJUSTE_END)
        if not _periodo_yyyymm_valido(periodo_ajuste, permitir_blanco=True, permitir_ceros=True):
            _add_issue(issues, "LSD-REG03-AJUSTE-001", linea=reg["linea"], cuil=reg.get("cuil"), detalle={"periodo_ajuste": periodo_ajuste})

    campos_reg04 = [
        ("rem_bruta", REG04_REM_BRUTA_START, REG04_REM_BRUTA_END),
        ("base1", REG04_BASE1_START, REG04_BASE1_END),
        ("base2", REG04_BASE2_START, REG04_BASE2_END),
        ("base3", REG04_BASE3_START, REG04_BASE3_END),
        ("base4", REG04_BASE4_START, REG04_BASE4_END),
        ("base5", REG04_BASE5_START, REG04_BASE5_END),
        ("base6", REG04_BASE6_START, REG04_BASE6_END),
        ("base7", REG04_BASE7_START, REG04_BASE7_END),
        ("base8", REG04_BASE8_START, REG04_BASE8_END),
        ("base9", REG04_BASE9_START, REG04_BASE9_END),
        ("dif_aporte_ss", REG04_DIF_APORTE_SS_START, REG04_DIF_APORTE_SS_END),
        ("dif_contr_ss", REG04_DIF_CONTR_SS_START, REG04_DIF_CONTR_SS_END),
        ("base10", REG04_BASE10_START, REG04_BASE10_END),
        ("importe_detraer", REG04_DETRACCION_START, REG04_DETRACCION_END),
    ]
    for reg in by_type.get("04", []):
        raw = reg["raw"]
        for campo, start, end in campos_reg04:
            _add_numeric_issue(issues, reg, campo, _slice(raw, start, end), end - start)

    for reg in by_type.get("05", []):
        raw = reg["raw"]
        _add_numeric_issue(issues, reg, "fecha_ingreso", _slice(raw, 23, 31), 8)
        _add_numeric_issue(issues, reg, "fecha_egreso", _slice(raw, 31, 39), 8, permitir_blanco=True)
        _add_numeric_issue(issues, reg, "importe", _slice(raw, 39, 54), 15)
        _add_numeric_issue(issues, reg, "cuit_eventual", _slice(raw, 54, 65), 11)

    for cuil, regs in analisis["bases_por_cuil"].items():
        if len(regs) > 1:
            _add_issue(issues, "LSD-REG04-DUP-001", cuil=cuil, detalle={"cantidad_reg04": len(regs), "lineas": [r["linea"] for r in regs]})

    for reg in registros:
        if reg["tipo"] in ("03", "04"):
            campos = reg["raw"][13:] if len(reg["raw"]) > 13 else ""
            if "," in campos:
                posiciones = [13 + i for i, c in enumerate(campos) if c == ","]
                _add_issue(issues, "LSD-NUM-COMMA-001", linea=reg["linea"], cuil=reg.get("cuil"), detalle={"tipo": reg["tipo"], "posiciones": posiciones[:10]})
            if re.search(r'\d[eE][+\-]?\d', campos):
                _add_issue(issues, "LSD-NUM-SCI-001", linea=reg["linea"], cuil=reg.get("cuil"), detalle={"tipo": reg["tipo"], "extracto": campos[:80]})
            if "-" in campos:
                _add_issue(issues, "LSD-NUM-NEG-001", linea=reg["linea"], cuil=reg.get("cuil"), detalle={"tipo": reg["tipo"]})

    for reg in reg02s:
        cbu = reg.get("cbu", "")
        cbu_limpio = cbu.strip()
        forma_pago = reg.get("forma_pago", "")
        if forma_pago == "3":
            if len(cbu) != 22 or not cbu.isdigit() or cbu == "0" * 22:
                _add_issue(issues, "LSD-REG02-CBU-001", linea=reg["linea"], cuil=reg["cuil"], detalle={"forma_pago": forma_pago, "cbu": cbu, "longitud": len(cbu)})
        elif cbu_limpio and not cbu.isdigit():
            _add_issue(issues, "LSD-REG02-CBU-001", linea=reg["linea"], cuil=reg["cuil"], detalle={"forma_pago": forma_pago, "cbu": cbu, "problema": "cbu_no_numerico_en_forma_pago_no_bancaria"})

    for reg in by_type.get("04", []):
        base4 = reg.get("base4")
        base10 = reg.get("base10")
        base2 = reg.get("base2")
        importe_detraer = reg.get("importe_detraer")
        rem = reg.get("rem_bruta")
        base1 = reg.get("base1")
        empleado = analisis["empleados"].get(reg["cuil"], {})
        legajo = (empleado.get("legajo") or "").strip()
        conceptos = analisis["conceptos_por_cuil"].get(reg["cuil"], [])
        concepto_0577 = _sumar_conceptos(conceptos, {"0577"})
        concepto_0448 = _sumar_conceptos(conceptos, {"0448"})
        conceptos_incremento_no_rem = _sumar_conceptos(conceptos, {"0525", "0535", "0536", "0537", "0538", "0539"})


        # 1. Validar Tope Máximo de Detracción (Por mes)
        mes_liq = "01"
        if reg01s and len(reg01s) > 0:
            per_str = reg01s[0].get("periodo", "")
            if len(per_str) == 6:
                mes_liq = per_str[4:6]
        
        tope_detraccion = Decimal("10558.05") if mes_liq in ("06", "12") else Decimal("7038.70")
        
        if importe_detraer is not None and importe_detraer > tope_detraccion:
            _add_issue(
                issues,
                "LSD-REG04-DETRACCION-MAX",
                linea=reg["linea"],
                cuil=reg["cuil"],
                detalle={"importe_detraer": str(importe_detraer), "tope_maximo": str(tope_detraccion)}
            )

        # 2. Validar Base 9 Inflada por Indemnizaciones (El error de los 1.6 millones)
        conceptos_infladores = {"0525", "0536", "0537", "0538", "0577", "0599"}
        suma_erronea = _sumar_conceptos(conceptos, conceptos_infladores)
        
        if base9 is not None and suma_erronea > Decimal("0") and base9 > (rem or Decimal("0")):
            _add_issue(
                issues,
                "LSD-REG04-BASE9-INDEM",
                linea=reg["linea"],
                cuil=reg["cuil"],
                detalle={
                    "base9_informada": str(base9),
                    "suma_erronea_detectada": str(suma_erronea),
                    "conceptos_culpables": list(conceptos_infladores)
                }
            )

        # 3. Validar Tope Proporcional de SAC Guillotinado (Ajustado para detectar 1 solo día)
        if base1 is not None and base1 > Decimal("1000000.00"):
            for concepto in conceptos:
                imp_con = concepto.get("importe", Decimal("0"))
                cant_raw = concepto.get("cantidad", "0")
                
                # Si el concepto paga más de 100k pero le informaron 15 días o menos
                if imp_con > Decimal("100000.00") and cant_raw.isdigit():
                    dias = Decimal(cant_raw) / Decimal("100")
                    if Decimal("0") < dias <= Decimal("15"):
                        _add_issue(
                            issues,
                            "LSD-REG04-TOPE-MOPRE-SAC",
                            linea=reg["linea"],
                            cuil=reg["cuil"],
                            detalle={
                                "base1_informada": str(base1),
                                "importe_sac": str(imp_con),
                                "dias_informados": str(dias)
                            }
                        )
                        break


        for campo in ("base1", "base2", "base3", "base4", "base5", "base6", "base7", "base8", "base9", "dif_aporte_ss", "dif_contr_ss", "base10", "importe_detraer"):
            valor = reg.get(campo)
            if valor is not None and valor < Decimal("0"):
                _add_issue(issues, "LSD-REG04-BASE-NEG-001", linea=reg["linea"], cuil=reg["cuil"], detalle={"campo": campo, "valor": reg.get(f"{campo}_raw", "")})
        if base4 == 0 and base10 not in (None, 0):
            _add_issue(issues, "LSD-REG04-BASE-001", linea=reg["linea"], cuil=reg["cuil"], detalle={"base4": reg["base4_raw"], "base10": reg["base10_raw"]})
        if rem == 0 and base1 and base1 > 0:
            _add_issue(issues, "LSD-REG04-REM-001", linea=reg["linea"], cuil=reg["cuil"], detalle={"rem_bruta": reg["rem_bruta_raw"], "base1": reg["base1_raw"], "problema": "rem_bruta_cero_base1_con_valor"})
        elif rem and base1 and base1 > rem:
            _add_issue(issues, "LSD-REG04-REM-001", linea=reg["linea"], cuil=reg["cuil"], detalle={"rem_bruta": reg["rem_bruta_raw"], "base1": reg["base1_raw"], "problema": "base1_supera_rem_bruta"})
        if base2 is not None and base10 is not None and importe_detraer is not None and importe_detraer > Decimal("0"):
            base10_esperada = max(base2 - importe_detraer, Decimal("0"))
            if abs(base10 - base10_esperada) > Decimal("0.05"):
                _add_issue(
                    issues,
                    "LSD-REG04-DETRACCION-001",
                    linea=reg["linea"],
                    cuil=reg["cuil"],
                    detalle={
                        "base": "10", 
                        "informado": str(base10),              
                        "determinado": str(base10_esperada),   
                        "diferencia": str(base10 - base10_esperada),
                        "base2": reg["base2_raw"],
                        "importe_detraer": reg["importe_detraer_raw"],
                        "base10": reg["base10_raw"],
                        "base10_esperada": str(base10_esperada),
                    },
                )

        if base1 is not None and reg.get("base9") is not None and concepto_0577 > Decimal("0") and reg["base9"] > base1:
            codigos_relacionados = {"0577"}
            _add_issue(
                issues,
                "LSD-REG04-BASES-CONCEPTOS-001",
                linea=reg["linea"],
                cuil=reg["cuil"],
                detalle={
                    "patron": "concepto_0577_con_base9_mayor_a_base1",
                    "legajo": legajo,
                    "base": 9,
                    "informado": str(reg["base9"]),
                    "determinado": str(base1),
                    "diferencia": str(reg["base9"] - base1),
                    "concepto_0577": str(concepto_0577),
                    "base1": reg["base1_raw"],
                    "base9": reg["base9_raw"],
                    "conceptos_relacionados": ["0577"],
                    "posiciones_usadas": {
                        "REG04 base1": "176-190",
                        "REG04 base9": "296-310",
                        "REG03 codigo_concepto": "14-23",
                        "REG03 importe": "30-44",
                        "REG03 debito_credito": "45",
                    },
                    "raw_reg04": reg.get("raw", ""),
                    "raw_reg03_relacionados": _raws_conceptos(conceptos, codigos_relacionados),
                },
            )
        if base1 is not None and base1 > Decimal("0") and reg.get("base9") is not None and conceptos_incremento_no_rem > Decimal("0") and reg["base9"] > (base1 * Decimal("2")):
            codigos_relacionados = {"0525", "0535", "0536", "0537", "0538", "0539"}
            _add_issue(
                issues,
                "LSD-REG04-BASES-CONCEPTOS-001",
                linea=reg["linea"],
                cuil=reg["cuil"],
                detalle={
                    "patron": "incrementos_no_remunerativos_inflando_base9",
                    "legajo": legajo,
                    "base": 9,
                    "informado": str(reg["base9"]),
                    "determinado": str(base1),
                    "diferencia": str(reg["base9"] - base1),
                    "conceptos_incremento": str(conceptos_incremento_no_rem),
                    "base1": reg["base1_raw"],
                    "base4": reg["base4_raw"],
                    "base5": reg["base5_raw"],
                    "base9": reg["base9_raw"],
                    "conceptos_relacionados": ["0525", "0535", "0536", "0537", "0538", "0539"],
                    "posiciones_usadas": {
                        "REG04 base1": "176-190",
                        "REG04 base4": "221-235",
                        "REG04 base5": "236-250",
                        "REG04 base9": "296-310",
                        "REG03 codigo_concepto": "14-23",
                        "REG03 importe": "30-44",
                        "REG03 debito_credito": "45",
                    },
                    "raw_reg04": reg.get("raw", ""),
                    "raw_reg03_relacionados": _raws_conceptos(conceptos, codigos_relacionados),
                },
            )
        if rem is not None and reg.get("base9") is not None and concepto_0448 > Decimal("0") and reg["base9"] > rem:
            codigos_relacionados = {"0448"}
            _add_issue(
                issues,
                "LSD-REG04-BASES-CONCEPTOS-001",
                linea=reg["linea"],
                cuil=reg["cuil"],
                detalle={
                    "patron": "concepto_0448_sumado_indebidamente_a_base9",
                    "legajo": legajo,
                    "base": 9,
                    "informado": str(reg["base9"]),
                    "determinado": str(rem),
                    "diferencia": str(reg["base9"] - rem),
                    "concepto_0448": str(concepto_0448),
                    "rem_bruta": reg["rem_bruta_raw"],
                    "base9": reg["base9_raw"],
                    "conceptos_relacionados": ["0448"],
                    "posiciones_usadas": {
                        "REG04 remuneracion_bruta": "161-175",
                        "REG04 base9": "296-310",
                        "REG03 codigo_concepto": "14-23",
                        "REG03 importe": "30-44",
                        "REG03 debito_credito": "45",
                    },
                    "raw_reg04": reg.get("raw", ""),
                    "raw_reg03_relacionados": _raws_conceptos(conceptos, codigos_relacionados),
                },
            )

        base5 = reg.get("base5")
        base9 = reg.get("base9")
        tolerancia = Decimal("0.01")


        if (
            base9 is not None and base2 is not None and base1 is not None
            and base9 == base2 and base1 < base2 - tolerancia
            and base4 is not None and abs(base4 - base1) <= tolerancia
        ):
            _add_issue(
                issues,
                "LSD-REG04-BASE9-003",
                linea=reg["linea"],
                cuil=reg["cuil"],
                detalle={
                    "base": 9,
                    "informado": str(base9),
                    "determinado": str(base1),
                    "diferencia": str(base9 - base1),
                    "base1": reg["base1_raw"],
                    "base2": reg["base2_raw"],
                    "base4": reg["base4_raw"],
                    "base9": reg["base9_raw"],
                },
            )

        if base2 is not None and base2 == 0 and base4 is not None and base4 > 0:
            _add_issue(
                issues,
                "LSD-REG04-BASE2-001",
                linea=reg["linea"],
                cuil=reg["cuil"],
                detalle={
                    "base2": reg["base2_raw"],
                    "base4": reg["base4_raw"],
                },
            )

        if importe_detraer is not None and base10 is not None:
            if importe_detraer == Decimal("0") and base10 > Decimal("0"):
                _add_issue(
                    issues,
                    "LSD-REG04-REM10-001",
                    linea=reg["linea"],
                    cuil=reg["cuil"],
                    detalle={
                        "importe_detraer": reg["importe_detraer_raw"],
                        "rem10": reg["base10_raw"],
                    },
                )

    periodo_mes = None
    if len(reg01s) == 1 and re.fullmatch(r'\d{6}', reg01s[0].get("periodo", "") or ""):
        periodo_mes = reg01s[0]["periodo"][4:6]
    for reg in by_type.get("03", []):
        cod = reg.get("codigo_arca", "")
        if cod == "5600000":
            _add_issue(issues, "LSD-REG03-CONCEPTO-001", linea=reg["linea"], cuil=reg["cuil"], detalle={"concepto": cod, "problema": "concepto_5600000"})
        if periodo_mes and periodo_mes not in ("06", "12") and cod.isdigit():
            cod_int = int(cod)
            if 1200000 <= cod_int <= 1299999 and cod != "1200030":
                _add_issue(issues, "LSD-REG03-SAC-001", linea=reg["linea"], cuil=reg["cuil"], detalle={"concepto": cod, "mes": periodo_mes})

    return issues

def ejecutar_validaciones_deterministicas(ruta: str) -> dict:
    analisis = obtener_analisis_lsd(ruta)
    issues = analisis["issues"]
    criticos = [i for i in issues if i.get("severidad") == "CRITICO"]
    advertencias = [i for i in issues if i.get("severidad") == "ADVERTENCIA"]
    return {
        "ok": not criticos,
        "ruta": analisis["ruta"],
        "size_bytes": analisis["size_bytes"],
        "total_lineas": analisis["total_lineas"],
        "lineas_vacias": analisis["lineas_vacias"],
        "registros_por_tipo": analisis["registros_por_tipo"],
        "indices": {
            "empleados": len(analisis["empleados"]),
            "cuils_con_conceptos": len(analisis["conceptos_por_cuil"]),
            "cuils_con_bases": len(analisis["bases_por_cuil"]),
            "cuils_con_eventuales": len(analisis["eventuales_por_cuil"]),
        },
        "issues": issues,
        "errores_criticos": len(criticos),
        "advertencias": len(advertencias),
        "catalogo_reglas": RULE_CATALOG,
    }

def _ok_sin_issues(analisis: dict, rule_ids: set[str]) -> bool:
    return not any(i.get("id") in rule_ids for i in analisis.get("issues", []))

def _detalle_issues(analisis: dict, rule_ids: set[str], limite: int = 30) -> list[dict]:
    return [i for i in analisis.get("issues", []) if i.get("id") in rule_ids][:limite]

# ---------------------------------------------------------------------------
# HERRAMIENTAS DE VALIDACIÓN
# ---------------------------------------------------------------------------

def tool_info_archivo(ruta: str) -> dict:
    try:
        analisis = obtener_analisis_lsd(ruta)
        return {
            "ok": True,
            "ruta": analisis["ruta"],
            "size_bytes": analisis["size_bytes"],
            "total_lineas": analisis["total_lineas"],
            "lineas_vacias": analisis["lineas_vacias"],
            "registros_por_tipo": analisis["registros_por_tipo"],
            "indices": {
                "empleados": len(analisis["empleados"]),
                "cuils_con_conceptos": len(analisis["conceptos_por_cuil"]),
                "cuils_con_bases": len(analisis["bases_por_cuil"]),
                "cuils_con_eventuales": len(analisis["eventuales_por_cuil"]),
            },
        }
    except FileNotFoundError:
        return {"ok": False, "error": f"Archivo no encontrado: {ruta}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_estructura(ruta: str) -> dict:
    try:
        analisis = obtener_analisis_lsd(ruta)
        rule_ids = {"LSD-REG01-STRUCT-001", "LSD-REG02-STRUCT-001", "LSD-CUIL-ORPHAN-001", "LSD-TIPO-UNKNOWN-001"}
        detalle = _detalle_issues(analisis, rule_ids)
        errores = [i for i in detalle if i.get("severidad") == "CRITICO"]
        advertencias = [i for i in detalle if i.get("severidad") == "ADVERTENCIA"]

        return {
            "ok": not errores,
            "errores": errores,
            "advertencias": advertencias,
            "resumen": {
                "reg01": analisis["registros_por_tipo"].get('01', 0),
                "reg02": analisis["registros_por_tipo"].get('02', 0),
                "reg03": analisis["registros_por_tipo"].get('03', 0),
                "reg04": analisis["registros_por_tipo"].get('04', 0),
                "reg05": analisis["registros_por_tipo"].get('05', 0),
                "cuils_unicos_reg02": len(analisis["empleados"]),
                "cuils_unicos_reg03": len(analisis["conceptos_por_cuil"]),
                "cuils_unicos_reg04": len(analisis["bases_por_cuil"]),
            }
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_duplicados_reg04(ruta: str) -> dict:
    try:
        analisis = obtener_analisis_lsd(ruta)
        issues = _detalle_issues(analisis, {"LSD-REG04-DUP-001"})
        duplicados = {
            i["cuil"]: {"cantidad_reg04": i["detalle"]["cantidad_reg04"], "en_lineas": i["detalle"]["lineas"]}
            for i in issues
        }
        return {
            "ok": len(duplicados) == 0,
            "total_reg04": analisis["registros_por_tipo"].get("04", 0),
            "cuils_unicos_reg04": len(analisis["bases_por_cuil"]),
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
        analisis = obtener_analisis_lsd(ruta)
        afectados = _detalle_issues(analisis, {"LSD-NUM-COMMA-001"}, 50)
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
        analisis = obtener_analisis_lsd(ruta)
        afectados = _detalle_issues(analisis, {"LSD-REG04-BASE-001"})
        correctos = [r["cuil"] for r in analisis["by_type"].get("04", []) if r.get("base4") not in (None, 0)]
        registro_corto = sum(1 for r in analisis["by_type"].get("04", []) if r["longitud"] <= REG04_BASE10_END)
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
        analisis = obtener_analisis_lsd(ruta)
        problemas = _detalle_issues(analisis, {"LSD-REG02-CBU-001"})
        forma_pago_3 = sum(1 for r in analisis["by_type"].get("02", []) if r.get("forma_pago") == "3")
        forma_pago_sin_cbu_obligatorio = sum(1 for r in analisis["by_type"].get("02", []) if r.get("forma_pago") != "3")
        return {
            "ok": len(problemas) == 0,
            "total_reg02": analisis["registros_por_tipo"].get("02", 0),
            "reg02_forma_pago_3": forma_pago_3,
            "reg02_sin_cbu_obligatorio": forma_pago_sin_cbu_obligatorio,
            "reg02_con_problema_cbu": len(problemas),
            "detalle": problemas[:30],
            "diagnostico": ("PROBLEMA CBU: Hay REG02 con CBU inválido según forma de pago." if problemas else "Sin problemas de CBU. OK.")
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_analizar_conceptos_reg03(ruta: str) -> dict:
    try:
        analisis = obtener_analisis_lsd(ruta)
        conteo: dict[str, int] = defaultdict(int)
        cuils_por_cod: dict[str, set] = defaultdict(set)
        for cuil, regs in analisis["conceptos_por_cuil"].items():
            for reg in regs:
                cod = reg.get("codigo_arca", "")
                cuils_por_cod[cod].add(cuil)
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
        analisis = obtener_analisis_lsd(ruta)
        reg01s = analisis["by_type"].get("01", [])
        if not reg01s:
            return {"ok": False, "error": "No se encontró REG01.", "diagnostico": "FALTA REG01."}
        cant_declarada = reg01s[0].get("cantidad_reg04")
        cant_real = analisis["registros_por_tipo"].get("04", 0)
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
        analisis = obtener_analisis_lsd(ruta)
        errores = _detalle_issues(analisis, {"LSD-LENGTH-001"})
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
        analisis = obtener_analisis_lsd(ruta)
        afectados = _detalle_issues(analisis, {"LSD-NUM-NEG-001"})
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
        analisis = obtener_analisis_lsd(ruta)
        afectados = _detalle_issues(analisis, {"LSD-NUM-SCI-001"})
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
        analisis = obtener_analisis_lsd(ruta)
        reg01s = analisis["by_type"].get("01", [])
        periodo_str = reg01s[0].get("periodo") if reg01s else None
        periodo_mes = periodo_str[4:6] if periodo_str and periodo_str.isdigit() else None
        if not periodo_mes:
            return {"ok": True, "advertencia": "No se pudo leer el mes del período desde REG01. Validación omitida."}
        if periodo_mes in ('06', '12'):
            return {"ok": True, "periodo": periodo_str, "mes": periodo_mes,
                    "diagnostico": f"Mes {periodo_mes} es de SAC — conceptos SAC semestrales permitidos."}
        afectados = _detalle_issues(analisis, {"LSD-REG03-SAC-001"})
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
        analisis = obtener_analisis_lsd(ruta)
        reg01s = analisis["by_type"].get("01", [])
        if not reg01s:
            return {"ok": False, "error": "No se encontró REG01.", "diagnostico": "FALTA REG01."}
        reg01 = reg01s[0]
        periodo = reg01.get("periodo", "")
        tipo_envio = reg01.get("tipo_envio", "")
        nro_presentacion = reg01.get("nro_presentacion", "")
        cuit_empleador = reg01.get("cuit_empleador", "")
        errores = _detalle_issues(analisis, {"LSD-REG01-PERIOD-001"})
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


def tool_validar_reg01_completo(ruta: str) -> dict:
    try:
        analisis = obtener_analisis_lsd(ruta)
        rule_ids = {"LSD-REG01-STRUCT-001", "LSD-REG01-COUNT04-001", "LSD-REG01-PERIOD-001", "LSD-REG01-CUIT-001", "LSD-REG01-LIQ-001"}
        issues = _detalle_issues(analisis, rule_ids)
        reg01s = analisis["by_type"].get("01", [])
        reg01 = reg01s[0] if reg01s else {}
        return {
            "ok": not any(i.get("severidad") == "CRITICO" for i in issues),
            "reg01": {
                "cuit_empleador": reg01.get("cuit_empleador"),
                "tipo_envio": reg01.get("tipo_envio"),
                "periodo": reg01.get("periodo"),
                "tipo_liquidacion": reg01.get("tipo_liquidacion"),
                "nro_presentacion": reg01.get("nro_presentacion"),
                "dias_base": reg01.get("dias_base"),
                "cantidad_reg04": reg01.get("cantidad_reg04"),
                "reg04_reales": analisis["registros_por_tipo"].get("04", 0),
            },
            "issues": issues,
            "diagnostico": "REG01 válido según layout y contenido real. OK." if not issues else "REG01 tiene inconsistencias determinísticas."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_validar_integridad_empleados(ruta: str) -> dict:
    try:
        analisis = obtener_analisis_lsd(ruta)
        rule_ids = {
            "LSD-CUIL-ORPHAN-001", "LSD-CUIL-FORMAT-001", "LSD-REG02-DUP-001",
            "LSD-EMP-INTEGRITY-001", "LSD-REG02-FECHA-001",
            "LSD-REG02-FORMA-PAGO-001", "LSD-REG02-CBU-001", "LSD-REG04-DUP-001",
        }
        issues = _detalle_issues(analisis, rule_ids, 100)
        return {
            "ok": not any(i.get("severidad") == "CRITICO" for i in issues),
            "empleados_reg02": len(analisis["empleados"]),
            "cuils_con_conceptos": len(analisis["conceptos_por_cuil"]),
            "cuils_con_bases": len(analisis["bases_por_cuil"]),
            "cuils_con_eventuales": len(analisis["eventuales_por_cuil"]),
            "issues": issues,
            "diagnostico": "Integridad por empleado correcta. OK." if not issues else "Hay problemas de integridad por empleado."
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
        analisis = obtener_analisis_lsd(ruta)
        afectados = _detalle_issues(analisis, {"LSD-REG04-REM-001"})
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
    _tool_def("validar_estructura",              "Valida la estructura: exactamente 1 REG01, existencia de REG02, que todo CUIL en REG03/REG04/REG05 tenga su REG02, y que no haya tipos desconocidos. REG05 corresponde a trabajadores eventuales y puede no existir."),
    _tool_def("validar_periodo_reg01",           "VAL-09: Valida que el período en REG01 (pos 16-21) sea AAAAMM válido, y verifica tipo de envío y N° de presentación."),
    _tool_def("validar_reg01_completo",          "Valida REG01 contra el layout oficial: CUIT empleador, período, tipo de envío, tipo de liquidación, número, días base y conteo REG04 real."),
    _tool_def("validar_conteo_reg04_en_reg01",   "VAL-01: Verifica que la cantidad de REG04 declarada en REG01 (pos 30-35) coincida exactamente con los REG04 reales. ARCA rechaza si difieren."),
    _tool_def("validar_longitud_registros",      "VAL-02: Verifica que cada línea tenga la longitud exacta de su tipo (REG01=35, REG02=115, REG03≥51, REG04≥370, REG05=65). ARCA rechaza longitudes incorrectas."),
    _tool_def("validar_integridad_empleados",    "Valida integridad por CUIL: REG02 existente, CUIL válido, conceptos con bases, duplicados, forma de pago, CBU y fechas."),
    _tool_def("validar_duplicados_reg04",        "Detecta Bug A: CUILs con más de un REG04 (empleado con dos legajos activos). ARCA suma todos → bases duplicadas (ratio 2,0x)."),
    _tool_def("validar_comas_numericos",         "Detecta Bug B (#30999): comas en campos numéricos de REG03/REG04. Formato correcto para ARCA es punto decimal sin separador de miles."),
    _tool_def("validar_valores_negativos",       "VAL-03: Detecta valores negativos en campos monetarios de REG03/REG04. El LSD no admite negativos; los descuentos se expresan con flag D/C."),
    _tool_def("validar_notacion_cientifica",     "VAL-06: Detecta notación científica (1.0E7) en campos numéricos. Bug H: ocurre cuando Rem. Total > $10.000.000. ARCA no puede parsear ese formato."),
    _tool_def("validar_bases_reg04",             "Detecta Bug C (Guía N°45): REG04 con Base4=0 y Base10 con valor → concepto 560.000 en lugar de 570.000 para docentes No-SIPA."),
    _tool_def("validar_rem_bruta_reg04",         "VAL-05: Verifica coherencia entre Rem. Bruta (pos 161-175) y Base 1 SIPA (pos 176-190) en REG04. La base no puede superar la remuneración."),
    _tool_def("validar_cbu",                     "Detecta CBU con formato incorrecto en REG02 posiciones 74-95. Es obligatorio y numérico de 22 dígitos cuando forma de pago es 3."),
    _tool_def("analizar_conceptos_reg03",        "Analiza conceptos ARCA en REG03: top-10, detección del concepto obsoleto 560.000 (Guía 45), conceptos presentes/faltantes."),
    _tool_def("validar_sac_fuera_de_periodo",    "VAL-07: Detecta conceptos SAC semestrales (120.000-129.999 excl. 120.003) en meses distintos de junio y diciembre. ARCA los rechaza."),
]

TOOL_FUNCTIONS = {
    "info_archivo":                  tool_info_archivo,
    "validar_estructura":            tool_validar_estructura,
    "validar_periodo_reg01":         tool_validar_periodo_reg01,
    "validar_reg01_completo":        tool_validar_reg01_completo,
    "validar_conteo_reg04_en_reg01": tool_validar_conteo_reg04_en_reg01,
    "validar_longitud_registros":    tool_validar_longitud_registros,
    "validar_integridad_empleados":  tool_validar_integridad_empleados,
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
# System prompt
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
           Posición 74-95: CBU (22 caracteres). Obligatorio numérico si forma de pago = 3.
           Posición 115: forma de pago (1=efectivo, 2=cheque, 3=acreditación en cuenta).

  REG03 — Conceptos remunerativos (N por empleado)
           Posición 2-12:  CUIL
           Posición 14-23: código de concepto del empleador (10 caracteres)
           Posición 24-28: cantidad
           Posición 29: unidad
           Posición 30-44: importe (13 enteros y 2 decimales, sin separadores)
           Posición 45: débito/crédito (D/C)
           Posición 46-51: período de ajuste AAAAMM o 000000

  REG04 — Bases imponibles por empleado (normalmente 1 por CUIL)
           Posición 2-12:  CUIL
           Posición ~220-234: Base 4 (aportes OS/FSR) — topeada con maxModPre
           Posición ~340-354: Base 10 = Rem10 (Ley 27.430) — Base2 - ImporteDetraccion

  REG05 — Trabajadores Eventuales (0..N registros, solo si corresponde)

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
