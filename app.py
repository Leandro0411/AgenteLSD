#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — Interfaz web Agente LSD v2  [adaptado a Google Gemini 2.5 Flash]
Flask + SSE — Solo diagnóstico y guía de pasos. La corrección la realiza el agente general externo.

REQUISITOS:
    pip install flask google-genai
    Variable de entorno: GEMINI_API_KEY=AIza...
"""
from dotenv import load_dotenv
load_dotenv()

import os, sys, json, tempfile, threading, queue, uuid, time
from collections import defaultdict
from flask import Flask, render_template, request, Response, jsonify, session

sys.path.insert(0, os.path.dirname(__file__))
from agente_lsd import (
    SYSTEM_PROMPT, MODELO, RULE_CATALOG,
    ejecutar_validaciones_deterministicas,
)
# correcciones.py desactivado — la corrección la realiza el agente general externo
try:
    from knowledge_loader import cargar_refs
except ImportError:
    def cargar_refs(): return []

from google import genai
from google.genai import types as gtypes

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

SESION_TTL_SEG = int(os.environ.get('SESION_TTL_SEG', '3600'))

# Sesiones en memoria: session_id → {ruta, informe, creado, ...}
SESIONES: dict[str, dict] = {}

TOOL_LABELS = {
    "info_archivo":                   "Leyendo el archivo...",
    "validar_estructura":             "Verificando estructura del archivo...",
    "validar_duplicados_reg04":       "Buscando empleados con legajos duplicados...",
    "validar_comas_numericos":        "Revisando formato de números...",
    "validar_bases_reg04":            "Verificando bases imponibles (Bug C / Guía 45)...",
    "validar_cbu":                    "Controlando CBUs...",
    "analizar_conceptos_reg03":       "Analizando conceptos declarados...",
    "validar_periodo_reg01":          "Verificando período y cabecera REG01...",
    "validar_reg01_completo":         "Validando cabecera REG01...",
    "validar_conteo_reg04_en_reg01":  "Controlando conteo de empleados declarado...",
    "validar_integridad_empleados":   "Validando integridad por empleado...",
    "validar_longitud_registros":     "Verificando longitud de cada registro...",
    "validar_valores_negativos":      "Buscando valores negativos en campos monetarios...",
    "validar_notacion_cientifica":    "Verificando campos numéricos (notación científica)...",
    "validar_rem_bruta_reg04":        "Controlando coherencia Rem. Bruta / Base SIPA...",
    "validar_sac_fuera_de_periodo":   "Verificando conceptos SAC en el período...",
}

# ── System prompts ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT_ANALISIS = SYSTEM_PROMPT + """

═══════════════════════════════════════════════════════════════
MODO INTERFAZ WEB — ANÁLISIS INICIAL
═══════════════════════════════════════════════════════════════
Respondé ÚNICAMENTE con un JSON válido, sin texto fuera del JSON, sin markdown.

IMPORTANTE — VALIDACIÓN DETERMINÍSTICA YA EJECUTADA:
Antes de que leas este mensaje, Python ya parseó el TXT una sola vez y ejecutó
el motor de reglas determinísticas. Los resultados llegan en el JSON
"Validación determinística".
TU ROL ES:
  1. Interpretar y explicar en lenguaje simple los issues del JSON determinístico.
  2. Incorporar el cruce con errores ARCA si viene incluido.
  3. NO inventar errores que no estén en "issues" ni en el cruce ARCA.
  4. Agrupar issues con el mismo "id" en un solo problema.
  5. Si no hay issues críticos ni advertencias, declarar el archivo presentable.

REGLAS DE LENGUAJE — MUY IMPORTANTE:
- Hablá como si le explicaras a un administrativo que nunca vio código.
- NO uses términos técnicos: nada de "REG04", "Base4", "CUIL_START", "topeMopreConver".
- SÍ podés decir: "empleado", "legajo", "CUIL", "número de CBU", "monto", "ARCA".
- Para cada problema: explicá QUÉ pasó, POR QUÉ pasó, y QUÉ hay que hacer, en ese orden.

═══════════════════════════════════════════════════════════════
REGLA CRÍTICA DE AGRUPACIÓN — OBLIGATORIA
═══════════════════════════════════════════════════════════════
NUNCA generes un problema separado por cada empleado.
Si 10 empleados tienen "Diferencia en Base 9", generás UN SOLO problema con:
  - todos los CUILs afectados en "todos_los_cuils_afectados"
  - cuils_afectados = 10
  - el detalle de diferencias de cada uno en "diferencias_por_cuil"

La lista "problemas" debe tener COMO MÁXIMO un elemento por tipo de error.
Tipos de error distintos = problemas distintos.
Múltiples empleados con el mismo error = UN solo problema.

═══════════════════════════════════════════════════════════════
INSTRUCCIONES PARA tutorial_pasos — MUY IMPORTANTE
═══════════════════════════════════════════════════════════════
Para CADA problema generá un campo "tutorial_pasos" con los pasos EXACTOS y
DETALLADOS para resolver el error en e-SUELDOS.

REGLAS PARA LOS PASOS:
- Mínimo 4 pasos, máximo 8. Nunca menos de 4.
- Cada paso describe UNA ACCIÓN CONCRETA. Si requiere más de una acción, dividí en dos pasos.
- Menciona el menú, botón o pantalla exacta de e-SUELDOS donde el consultor tiene que ir.
- Si el error requiere recalcular en e-SUELDOS, decilo explícitamente en qué pantalla y con qué botón.
- Si hay un concepto ARCA que hay que crear o modificar, detallá los campos a completar.
- Si aplica, indicá cómo VERIFICAR que el paso funcionó (qué tiene que ver el consultor en pantalla).
- Basate SIEMPRE en los PDFs de normativa adjuntos, en especial "Libro Sueldo Digital - Errores Frecuentes LSD".
- Si el error está documentado en ese PDF, citá el nombre del instructivo en el campo "referencia".
- El último paso debe ser siempre: "Volvé a exportar el LSD y validalo de nuevo con el Agente".

Ejemplo de paso bien redactado:
{
  "paso": 2,
  "titulo": "Acceder al módulo de conceptos",
  "instruccion": "En e-SUELDOS, andá a Configuración → AFIP → Conceptos ARCA. Buscá el concepto que usás para docentes No-SIPA (probablemente figura como '560.000'). Hacé clic en Editar.",
  "referencia": "Libro Sueldo Digital - Errores Frecuentes LSD — sección Concepto 570.000"
}

Estructura exacta del JSON de respuesta:
{
  "resumen": "2-3 oraciones en lenguaje simple sobre el estado del archivo",
  "veredicto": "PRESENTABLE" | "SERÁ RECHAZADO" | "REVISAR",
  "veredicto_razon": "Una oración simple explicando el veredicto",
  "estadisticas": {
    "total_empleados": N,
    "total_conceptos": N,
    "errores_criticos": N,
    "advertencias": N
  },
  "errores_arca": {
    "presente": true | false,
    "total": N,
    "resumen": "Resumen en lenguaje simple de los errores ARCA (si hay archivo de errores)"
  },
  "problemas": [
    {
      "id": "bug_a" | "bug_b" | "bug_c" | "base9_detraccion" | "base1_sipa" | "base4_os" | "estructura" | "cbu" | "otro_001" (usar IDs únicos y descriptivos),
      "severidad": "CRITICO" | "ADVERTENCIA" | "INFO",
      "titulo": "Título claro en lenguaje simple (máx 8 palabras)",
      "descripcion": "Qué está pasando, explicado para un administrativo",
      "cuils_afectados": N,
      "todos_los_cuils_afectados": ["XX-XXXXXXXX-X", ...],
      "ejemplos_cuil": ["XX-XXXXXXXX-X"],
      "causa": "Por qué ocurre este error (1-2 oraciones simples)",
      "solucion": "Resumen de qué hay que hacer (1-2 oraciones)",
      "tutorial_pasos": [
        {
          "paso": 1,
          "titulo": "Título corto y accionable del paso",
          "instruccion": "Descripción muy detallada y concreta. Mencioná el menú exacto de e-SUELDOS, el botón a presionar, el campo a completar y qué resultado se espera ver en pantalla.",
          "referencia": "Nombre exacto del PDF o sección del instructivo que respalda este paso"
        }
      ],
      "diagnostico_cruce": "Solo si viene del cruce errores ARCA + LSD: explicación de la causa raíz",
      "diferencias_por_cuil": [
        {
          "cuil": "XXXXXXXXXXX",
          "base": N,
          "informado": N.NN,
          "determinado": N.NN,
          "diferencia": N.NN,
          "interpretacion": "Explicación simple de por qué difiere este empleado en particular"
        }
      ]
    }
  ]
}

REGLAS ADICIONALES:
- tutorial_pasos: mínimo 4 pasos por problema, máximo 8. El último paso siempre es "Volvé a exportar y validar con el Agente".
- todos_los_cuils_afectados: incluí TODOS los CUILs con ese error (no solo ejemplos).
- NO incluir campos auto_corregible, accion_correccion, cambios_propuestos ni advertencia_correccion — esos campos fueron removidos.
"""


# ── Sesiones y temporales ──────────────────────────────────────────────────────

def _limpiar_sesiones_expiradas() -> None:
    ahora = time.time()
    expiradas = [
        sid for sid, ses in SESIONES.items()
        if ahora - ses.get('creado', ahora) > SESION_TTL_SEG
    ]
    for sid in expiradas:
        _eliminar_sesion(sid)


def _eliminar_sesion(session_id: str) -> None:
    sesion = SESIONES.pop(session_id, None)
    if not sesion:
        return
    for clave in ('ruta',):
        ruta = sesion.get(clave)
        if ruta and os.path.isfile(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass


INFORME_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "resumen": {"type": "string"},
        "veredicto": {"type": "string", "enum": ["PRESENTABLE", "SERÁ RECHAZADO", "REVISAR"]},
        "veredicto_razon": {"type": "string"},
        "estadisticas": {
            "type": "object",
            "properties": {
                "total_empleados": {"type": "integer"},
                "total_conceptos": {"type": "integer"},
                "errores_criticos": {"type": "integer"},
                "advertencias": {"type": "integer"},
            },
            "required": ["total_empleados", "total_conceptos", "errores_criticos", "advertencias"],
        },
        "errores_arca": {
            "type": "object",
            "properties": {
                "presente": {"type": "boolean"},
                "total": {"type": "integer"},
                "resumen": {"type": "string"},
            },
            "required": ["presente", "total", "resumen"],
        },
        "problemas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "severidad": {"type": "string", "enum": ["CRITICO", "ADVERTENCIA", "INFO"]},
                    "titulo": {"type": "string"},
                    "descripcion": {"type": "string"},
                    "cuils_afectados": {"type": "integer"},
                    "todos_los_cuils_afectados": {"type": "array", "items": {"type": "string"}},
                    "ejemplos_cuil": {"type": "array", "items": {"type": "string"}},
                    "causa": {"type": "string"},
                    "solucion": {"type": "string"},
                    "tutorial_pasos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "paso": {"type": "integer"},
                                "titulo": {"type": "string"},
                                "instruccion": {"type": "string"},
                                "referencia": {"type": "string"},
                            },
                            "required": ["paso", "titulo", "instruccion", "referencia"],
                        },
                    },
                    "diagnostico_cruce": {"type": "string"},
                    "diferencias_por_cuil": {"type": "array", "items": {"type": "object"}},
                },
                "required": [
                    "id", "severidad", "titulo", "descripcion", "cuils_afectados",
                    "todos_los_cuils_afectados", "causa", "solucion", "tutorial_pasos",
                ],
            },
        },
    },
    "required": ["resumen", "veredicto", "veredicto_razon", "estadisticas", "errores_arca", "problemas"],
}


# ── Cliente Gemini ─────────────────────────────────────────────────────────────

def _get_client() -> genai.Client | None:
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        return None
    return genai.Client(api_key=api_key)


def _cargar_pdf_parts() -> list[gtypes.Part]:
    """Carga los PDFs de normativa subidos previamente con knowledge_loader.py"""
    refs = cargar_refs()
    parts = []
    for ref in refs:
        try:
            parts.append(gtypes.Part.from_uri(
                file_uri=ref["uri"],
                mime_type="application/pdf"
            ))
        except Exception:
            pass
    return parts


def _extraer_texto_respuesta(response) -> str | None:
    """Extrae el texto de la respuesta de Gemini."""
    try:
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text and part.text.strip():
                return part.text.strip()
    except Exception:
        pass
    return None


def _extraer_fn_calls(response) -> list:
    """Extrae los function calls de la respuesta de Gemini."""
    try:
        return [
            part.function_call
            for part in response.candidates[0].content.parts
            if hasattr(part, "function_call") and part.function_call
        ]
    except Exception:
        return []


def _es_fin(response) -> bool:
    """Determina si Gemini terminó de generar."""
    try:
        return response.candidates[0].finish_reason.name in ("STOP", "MAX_TOKENS")
    except Exception:
        return True


def _informe_basico_deterministico(validacion: dict) -> dict:
    problemas = _construir_problemas_desde_validacion(validacion)
    veredicto = _veredicto_desde_validacion(validacion)
    return {
        "resumen": _resumen_desde_validacion(validacion, problemas, veredicto),
        "veredicto": veredicto,
        "veredicto_razon": _razon_veredicto(validacion, problemas, veredicto),
        "estadisticas": _calcular_estadisticas_informe(validacion, problemas),
        "errores_arca": {"presente": False, "total": 0, "resumen": ""},
        "problemas": problemas,
    }


def _veredicto_desde_validacion(validacion: dict) -> str:
    if validacion.get("errores_criticos", 0) > 0:
        return "SERÁ RECHAZADO"
    if validacion.get("advertencias", 0) > 0:
        return "REVISAR"
    return "PRESENTABLE"


def _construir_problemas_desde_validacion(
    validacion: dict,
    tutoriales_por_id: dict[str, list] | None = None,
) -> list[dict]:
    issues = validacion.get("issues", [])
    agrupados: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        agrupados[issue["id"]].append(issue)

    tutoriales_por_id = tutoriales_por_id or {}
    problemas = []
    for rule_id, items in sorted(agrupados.items()):
        first = items[0]
        regla = RULE_CATALOG.get(rule_id, {})
        cuils = sorted({i.get("cuil") for i in items if i.get("cuil")})
        mensaje = regla.get("mensaje") or first.get("mensaje", "")
        fix_hint = regla.get("fix_hint") or first.get("fix_hint", "")
        tutorial = tutoriales_por_id.get(rule_id) or _tutorial_basico(mensaje, fix_hint, regla.get("fuente_pdf", ""))
        problemas.append({
            "id": rule_id,
            "severidad": regla.get("severidad") or first.get("severidad", "INFO"),
            "titulo": mensaje,
            "descripcion": mensaje,
            "cuils_afectados": len(cuils),
            "todos_los_cuils_afectados": cuils,
            "ejemplos_cuil": cuils[:5],
            "causa": mensaje,
            "solucion": fix_hint,
            "tutorial_pasos": tutorial,
            "diagnostico_cruce": "",
            "diferencias_por_cuil": _diferencias_desde_issues(items),
            "detalle_tecnico": _detalle_tecnico_problema(rule_id, items),
        })
    return problemas


def _tutorial_basico(mensaje: str, fix_hint: str, fuente: str) -> list[dict]:
    return [
        {"paso": 1, "titulo": "Revisar el origen", "instruccion": mensaje or "Revisá el dato indicado.", "referencia": fuente},
        {"paso": 2, "titulo": "Corregir en e-Sueldos", "instruccion": fix_hint or "Corregí el dato en e-Sueldos.", "referencia": fuente},
        {"paso": 3, "titulo": "Recalcular", "instruccion": "Recalculá la liquidación o el Libro Sueldo Digital desde e-Sueldos.", "referencia": fuente},
        {"paso": 4, "titulo": "Validar nuevamente", "instruccion": "Volvé a exportar el LSD y validalo de nuevo con el Agente.", "referencia": fuente},
    ]


def _calcular_estadisticas_informe(validacion: dict, problemas: list[dict]) -> dict:
    issues = validacion.get("issues", [])
    stats = validacion.get("indices", {})
    total_emp = stats.get("empleados", 0)

    cuils_crit = {i.get("cuil") for i in issues if i.get("severidad") == "CRITICO" and i.get("cuil")}
    cuils_adv = {i.get("cuil") for i in issues if i.get("severidad") == "ADVERTENCIA" and i.get("cuil")}
    cuils_adv_solo = cuils_adv - cuils_crit
    cuils_afectados = cuils_crit | cuils_adv_solo

    tipos_crit = sum(1 for p in problemas if (p.get("severidad") or "").upper() == "CRITICO")
    tipos_adv = sum(1 for p in problemas if (p.get("severidad") or "").upper() == "ADVERTENCIA")
    det_crit = sum(1 for i in issues if i.get("severidad") == "CRITICO")
    det_adv = sum(1 for i in issues if i.get("severidad") == "ADVERTENCIA")

    return {
        "total_empleados": total_emp,
        "total_conceptos": validacion.get("registros_por_tipo", {}).get("03", 0),
        "empleados_afectados_critico": len(cuils_crit),
        "empleados_afectados_advertencia": len(cuils_adv_solo),
        "empleados_validados": max(total_emp - len(cuils_afectados), 0),
        "tipos_error_critico": tipos_crit,
        "tipos_error_advertencia": tipos_adv,
        "detecciones_criticas": det_crit,
        "detecciones_advertencia": det_adv,
        # Compatibilidad con historial / código legado
        "errores_criticos": len(cuils_crit),
        "advertencias": len(cuils_adv_solo),
    }


def _razon_veredicto(validacion: dict, problemas: list[dict], veredicto: str) -> str:
    stats = _calcular_estadisticas_informe(validacion, problemas)
    total_emp = stats["total_empleados"]
    if veredicto == "PRESENTABLE":
        return f"Los {total_emp} empleados del archivo pasaron las validaciones determinísticas."
    if veredicto == "REVISAR":
        adv = stats["empleados_afectados_advertencia"]
        tipos = stats["tipos_error_advertencia"]
        return f"{adv} empleado(s) con advertencia · {tipos} tipo(s) de problema · {stats['detecciones_advertencia']} detecciones"
    crit = stats["empleados_afectados_critico"]
    tipos = stats["tipos_error_critico"]
    det = stats["detecciones_criticas"]
    return f"{crit} de {total_emp} empleados con error crítico · {tipos} tipo(s) de problema · {det} detecciones"


def _resumen_desde_validacion(validacion: dict, problemas: list[dict], veredicto: str) -> str:
    if veredicto == "PRESENTABLE":
        return "El archivo respeta las reglas determinísticas del layout LSD validadas por el agente."
    if veredicto == "REVISAR":
        return "El archivo no tiene errores críticos determinísticos, pero conviene revisar las advertencias antes de presentar."
    tipos = _calcular_estadisticas_informe(validacion, problemas)["tipos_error_critico"]
    return (
        f"Se detectaron inconsistencias en bases imponibles y REG04 en {tipos} tipo(s) de regla. "
        "Corregilas en e-Sueldos y regenerá el LSD antes de presentar a ARCA."
    )


def _finalizar_informe(informe: dict, validacion: dict) -> dict:
    """Reemplaza problemas y métricas con datos determinísticos; conserva tutorial_pasos de la IA."""
    tutoriales = {
        p.get("id"): p.get("tutorial_pasos")
        for p in (informe.get("problemas") or [])
        if p.get("id") and p.get("tutorial_pasos")
    }
    problemas = _construir_problemas_desde_validacion(validacion, tutoriales)
    veredicto = _veredicto_desde_validacion(validacion)
    informe["problemas"] = problemas
    informe["veredicto"] = veredicto
    informe["veredicto_razon"] = _razon_veredicto(validacion, problemas, veredicto)
    informe["estadisticas"] = _calcular_estadisticas_informe(validacion, problemas)
    informe["resumen"] = _resumen_desde_validacion(validacion, problemas, veredicto)
    return _adjuntar_detalle_tecnico(informe, validacion)


def _detalle_tecnico_problema(rule_id: str, items: list[dict], limite: int = 30) -> dict:
    first = items[0] if items else {}
    evidencias = []
    for item in items[:limite]:
        detalle = item.get("detalle") or {}
        evidencia = {
            "linea": item.get("linea"),
            "cuil": item.get("cuil"),
            "detalle": detalle,
        }
        if "raw_reg04" in detalle:
            evidencia["raw_reg04"] = detalle.get("raw_reg04")
        if "raw_reg03_relacionados" in detalle:
            evidencia["raw_reg03_relacionados"] = detalle.get("raw_reg03_relacionados") or []
        if "posiciones_usadas" in detalle:
            evidencia["posiciones_usadas"] = detalle.get("posiciones_usadas") or {}
        evidencias.append(evidencia)

    return {
        "regla_id": rule_id,
        "severidad": first.get("severidad"),
        "campo": first.get("campo"),
        "fuente_pdf": first.get("fuente_pdf"),
        "mensaje_regla": first.get("mensaje"),
        "fix_hint": first.get("fix_hint"),
        "total_evidencias": len(items),
        "evidencias": evidencias,
    }


def _diferencias_desde_issues(items: list[dict]) -> list[dict]:
    diferencias = []
    for item in items:
        detalle = item.get("detalle") or {}
        if not any(k in detalle for k in ("informado", "determinado", "diferencia", "base")):
            continue
        diferencias.append({
            "legajo": detalle.get("legajo") or "—",
            "cuil": item.get("cuil") or "—",
            "base": detalle.get("base") or "—",
            "informado": detalle.get("informado") or "—",
            "determinado": detalle.get("determinado") or "—",
            "diferencia": detalle.get("diferencia") or "—",
        })
    return diferencias


def _fila_diferencia_util(fila: dict) -> bool:
    return any(
        fila.get(k) not in (None, "", "—")
        for k in ("legajo", "base", "informado", "determinado", "diferencia")
    )


def _adjuntar_detalle_tecnico(informe: dict, validacion: dict) -> dict:
    issues = validacion.get("issues", [])
    agrupados: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        agrupados[issue.get("id", "")].append(issue)
    for problema in informe.get("problemas", []) or []:
        rule_id = problema.get("id")
        if rule_id and rule_id in agrupados:
            problema["detalle_tecnico"] = _detalle_tecnico_problema(rule_id, agrupados[rule_id])
            filas_actuales = problema.get("diferencias_por_cuil") or []
            if not filas_actuales or not any(_fila_diferencia_util(f) for f in filas_actuales):
                problema["diferencias_por_cuil"] = _diferencias_desde_issues(agrupados[rule_id])
    return informe


# ── Fase 1: Análisis ──────────────────────────────────────────────────────────

def ejecutar_analisis(session_id: str, q: queue.Queue):
    """Fase 1: validación determinística + explicación con Gemini."""
    sesion = SESIONES.get(session_id)
    if not sesion:
        q.put({"tipo": "error", "mensaje": "Sesión no encontrada."}); q.put(None); return

    ruta          = sesion['ruta']
    modo          = sesion.get('modo', 'auto')

    q.put({"tipo": "herramienta", "nombre": "validacion_deterministica",
           "label": "Ejecutando validaciones determinísticas..."})
    try:
        deterministico = ejecutar_validaciones_deterministicas(ruta)
    except Exception as exc:
        q.put({"tipo": "error", "mensaje": f"Error validando TXT: {exc}"}); q.put(None); return

    sesion['validacion_deterministica'] = deterministico

    if modo == "rapido":
        informe = _informe_basico_deterministico(deterministico)
        informe["modo_analisis"] = "rapido"
        sesion['informe'] = informe
        q.put({"tipo": "informe", "data": informe})
        q.put(None)
        return

    if modo == "auto" and deterministico.get("errores_criticos", 0) == 0 and deterministico.get("advertencias", 0) == 0:
        informe = _informe_basico_deterministico(deterministico)
        informe["modo_analisis"] = "auto_sin_ia"
        sesion['informe'] = informe
        q.put({"tipo": "informe", "data": informe})
        q.put(None)
        return

    client = _get_client()
    if not client:
        informe = _informe_basico_deterministico(deterministico)
        sesion['informe'] = informe
        q.put({"tipo": "informe", "data": informe})
        q.put(None)
        return

    q.put({"tipo": "herramienta", "nombre": "explicacion_ia",
           "label": "Preparando informe con IA..."})

    # La normativa se usa como catálogo local versionado en RULE_CATALOG/reglas_lsd.json.
    # No adjuntamos PDFs en cada análisis: la IA solo explica el JSON determinístico.
    pdf_parts = []

    msg_texto = (
        "Convertí el siguiente resultado de validación determinística en el informe JSON estructurado "
        "de la interfaz. NO ejecutes validaciones nuevas ni inventes errores: usá únicamente los issues "
        "del JSON determinístico.\n\n"
        f"Archivo LSD: {os.path.abspath(ruta)}\n"
        f"Validación determinística:\n{json.dumps(deterministico, ensure_ascii=False, indent=2)}\n\n"
    )
    msg_texto += "Respondé ÚNICAMENTE con el JSON estructurado solicitado por el sistema."

    if pdf_parts:
        msg_texto += f"\n\nAdjunto {len(pdf_parts)} documentos de normativa LSD para enriquecer el diagnóstico."

    config = gtypes.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT_ANALISIS,
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=INFORME_RESPONSE_SCHEMA,
    )

    # Historial: texto + PDFs opcionales en el primer mensaje
    historial: list[gtypes.Content] = [
        gtypes.Content(
            role="user",
            parts=pdf_parts + [gtypes.Part.from_text(text=msg_texto)]
        )
    ]

    resp = None
    for intento in range(4):
        try:
            resp = client.models.generate_content(
                model=MODELO,
                contents=historial,
                config=config,
            )
            break
        except Exception as e:
            msg = str(e)
            es_transitorio = any(c in msg for c in ('503', '429', '500', 'UNAVAILABLE', 'overloaded'))
            if es_transitorio and intento < 3:
                espera = 5 * (2 ** intento)
                q.put({"tipo": "aviso", "mensaje": f"Gemini ocupado, reintentando en {espera}s... (intento {intento+2}/4)"})
                time.sleep(espera)
            else:
                q.put({"tipo": "error", "mensaje": msg}); q.put(None); return

    historial.append(resp.candidates[0].content)
    texto = _extraer_texto_respuesta(resp)
    if texto:
        if texto.startswith("```"):
            partes = texto.split("```")
            texto = partes[1] if len(partes) > 1 else texto
            if texto.startswith("json"):
                texto = texto[4:]
        try:
            informe = json.loads(texto.strip())
            informe = _finalizar_informe(informe, deterministico)
            sesion['informe'] = informe
            sesion['historial_analisis'] = historial
            q.put({"tipo": "informe", "data": informe})
        except json.JSONDecodeError:
            q.put({"tipo": "texto_libre", "data": texto})
    else:
        q.put({"tipo": "error", "mensaje": "El modelo no devolvió respuesta. Intentá de nuevo."})

    q.put(None)


# ejecutar_correccion() eliminada — la corrección la realiza el agente general externo


# ── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analizar', methods=['POST'])
def analizar():
    _limpiar_sesiones_expiradas()
    if 'archivo' not in request.files:
        return jsonify({"error": "No se recibió archivo"}), 400
    archivo = request.files['archivo']
    if not archivo.filename.lower().endswith('.txt'):
        return jsonify({"error": "El archivo debe ser .txt"}), 400
    modo = request.form.get('modo', 'auto')
    if modo not in ('auto', 'rapido', 'profundo'):
        modo = 'auto'

    with tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False, prefix='lsd_') as tmp:
        archivo.save(tmp)
        ruta_tmp = tmp.name

    session_id = str(uuid.uuid4())
    SESIONES[session_id] = {
        'ruta':              ruta_tmp,
        'modo':              modo,
        'informe':           None,
        'historial_analisis': [],
        'creado':            time.time(),
    }

    def generar():
        q = queue.Queue()
        t = threading.Thread(target=ejecutar_analisis, args=(session_id, q), daemon=True)
        t.start()
        yield f"data: {json.dumps({'tipo': 'session', 'session_id': session_id})}\n\n"
        while True:
            try:
                ev = q.get(timeout=120)
            except queue.Empty:
                yield f"data: {json.dumps({'tipo': 'error', 'mensaje': 'Tiempo agotado'})}\n\n"; break
            if ev is None:
                yield f"data: {json.dumps({'tipo': 'fin'})}\n\n"; break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return Response(generar(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# Rutas /corregir y /descargar eliminadas — la corrección la realiza el agente general externo


@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json(silent=True) or {}
    messages = data.get('messages') or []
    session_id = data.get('session_id')
    informe_cliente = data.get('informe')
    nombre_archivo = data.get('archivo')

    client = _get_client()
    if not client:
        return jsonify({"respuesta": "La API Key de Gemini no está configurada."}), 503

    sesion = SESIONES.get(session_id) if session_id else None
    informe = sesion.get('informe') if sesion else None
    if not informe:
        informe = informe_cliente

    contexto = {
        "archivo": nombre_archivo,
        "hay_txt_cargado": bool(sesion),
        "informe": informe,
        "validacion_deterministica": sesion.get('validacion_deterministica') if sesion else None,
    }

    if sesion and sesion.get('ruta') and os.path.exists(sesion['ruta']):
        try:
            with open(sesion['ruta'], encoding='latin-1') as f:
                lineas = [l.rstrip('\r\n') for l in f]
            contexto["txt"] = {
                "total_lineas": len(lineas),
                "primeras_lineas": lineas[:25],
                "ultimas_lineas": lineas[-15:],
            }
        except Exception:
            pass

    system_chat = SYSTEM_PROMPT + """

═══════════════════════════════════════════════════════════════
MODO CHAT CONTEXTUAL
═══════════════════════════════════════════════════════════════
Respondé en español, claro y directo. El usuario puede preguntar sobre:
- estructura del TXT de Libro Sueldo Digital,
- reglas generales de ARCA/LSD,
- el archivo cargado y el informe generado en esta sesión.

No inventes datos del TXT. Si falta contexto, decilo y pedí que primero cargue o analice un archivo.
Para preguntas técnicas podés mencionar REG01/REG02/REG03/REG04/REG05, posiciones y campos.
Recordatorio de formato oficial relevante:
- REG02 contiene CBU en posiciones 74-95 y forma de pago en posición 115.
- REG05 corresponde a trabajadores eventuales; puede no existir si no corresponde.
"""

    contenido = [
        gtypes.Part.from_text(text="Contexto disponible de la sesión:\n" + json.dumps(contexto, ensure_ascii=False, indent=2))
    ]
    historial_chat: list[gtypes.Content] = [
        gtypes.Content(role="user", parts=contenido)
    ]
    for m in messages[-12:]:
        role = "model" if m.get("role") == "assistant" else "user"
        text = str(m.get("content") or "").strip()
        if text:
            historial_chat.append(gtypes.Content(
                role=role,
                parts=[gtypes.Part.from_text(text=text)],
            ))

    try:
        resp = client.models.generate_content(
            model=MODELO,
            contents=historial_chat,
            config=gtypes.GenerateContentConfig(
                system_instruction=system_chat,
                temperature=0.2,
            ),
        )
        texto = _extraer_texto_respuesta(resp) or "No pude generar una respuesta."
        return jsonify({"respuesta": texto})
    except Exception as e:
        return jsonify({"respuesta": f"Error consultando a Gemini: {e}"}), 500


@app.route('/health')
def health():
    api_key = os.environ.get('GEMINI_API_KEY')
    pdf_refs = cargar_refs()
    return jsonify({
        "ok": True,
        "api_configurada": bool(api_key),
        "modelo": MODELO,
        "pdfs_normativa": len(pdf_refs),
    })


if __name__ == '__main__':
    print("\n" + "="*55)
    print(f"  AGENTE LSD — Interfaz Web v2  [{MODELO}]")
    print("="*55)
    if not os.environ.get('GEMINI_API_KEY'):
        print("\n  ⚠  GEMINI_API_KEY no configurada.")
        print("     Obtené tu key en: https://aistudio.google.com/apikey")
        print("     Windows: setx GEMINI_API_KEY \"AIza...\"")
        print("     Linux/Mac: export GEMINI_API_KEY=AIza...")
    pdf_refs = cargar_refs()
    if pdf_refs:
        print(f"\n  📚 {len(pdf_refs)} PDFs de normativa cargados.")
    else:
        print("\n  ℹ  Sin PDFs de normativa. Para agregar: python knowledge_loader.py")
    print("\n  Abrí tu navegador en:  http://localhost:5000")
    print("="*55 + "\n")
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
