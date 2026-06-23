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
    TOOL_DECLARATIONS, TOOL_FUNCTIONS,
    TOOL_DECLARATIONS_ALL, TOOL_FUNCTIONS_ALL,
    SYSTEM_PROMPT, MODELO,
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
app.secret_key = os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# Sesiones en memoria: session_id → {ruta_tmp, ruta_errores, messages, estado}
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
    "parsear_errores_arca":           "Leyendo errores de ARCA...",
    "cruzar_errores_con_lsd":         "Cruzando errores ARCA con el libro digital...",
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


def _informe_basico_deterministico(validacion: dict, arca_contexto: dict | None = None) -> dict:
    issues = validacion.get("issues", [])
    criticos = [i for i in issues if i.get("severidad") == "CRITICO"]
    advertencias = [i for i in issues if i.get("severidad") == "ADVERTENCIA"]
    stats = validacion.get("indices", {})
    problemas = []
    agrupados: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        agrupados[issue["id"]].append(issue)

    for rule_id, items in agrupados.items():
        first = items[0]
        cuils = sorted({i.get("cuil") for i in items if i.get("cuil")})
        problemas.append({
            "id": rule_id,
            "severidad": first.get("severidad", "INFO"),
            "titulo": first.get("campo", rule_id),
            "descripcion": first.get("mensaje", ""),
            "cuils_afectados": len(cuils),
            "todos_los_cuils_afectados": cuils,
            "ejemplos_cuil": cuils[:5],
            "causa": first.get("mensaje", ""),
            "solucion": first.get("fix_hint", ""),
            "tutorial_pasos": [
                {
                    "paso": 1,
                    "titulo": "Revisar el origen",
                    "instruccion": first.get("mensaje", "Revisá el dato indicado por la validación."),
                    "referencia": first.get("fuente_pdf", ""),
                },
                {
                    "paso": 2,
                    "titulo": "Corregir en e-Sueldos",
                    "instruccion": first.get("fix_hint", "Corregí el dato en e-Sueldos."),
                    "referencia": first.get("fuente_pdf", ""),
                },
                {
                    "paso": 3,
                    "titulo": "Recalcular",
                    "instruccion": "Recalculá la liquidación o el Libro Sueldo Digital desde e-Sueldos.",
                    "referencia": first.get("fuente_pdf", ""),
                },
                {
                    "paso": 4,
                    "titulo": "Validar nuevamente",
                    "instruccion": "Volvé a exportar el LSD y validalo de nuevo con el Agente.",
                    "referencia": first.get("fuente_pdf", ""),
                },
            ],
            "diagnostico_cruce": "",
            "diferencias_por_cuil": [],
            "detalle_tecnico": _detalle_tecnico_problema(rule_id, items),
        })

    veredicto = "SERÁ RECHAZADO" if criticos else ("REVISAR" if advertencias else "PRESENTABLE")
    if veredicto == "PRESENTABLE":
        razon = "El motor determinístico no encontró errores críticos ni advertencias."
        resumen = "El archivo respeta las reglas determinísticas del layout LSD validadas por el agente."
    elif veredicto == "REVISAR":
        razon = f"Hay {len(advertencias)} advertencia(s) para revisar antes de presentar."
        resumen = "El archivo no tiene errores críticos determinísticos, pero conviene revisar las advertencias."
    else:
        razon = f"Hay {len(criticos)} error(es) crítico(s) determinísticos."
        resumen = "El archivo tiene errores críticos de estructura o formato que pueden causar rechazo."

    return {
        "resumen": resumen,
        "veredicto": veredicto,
        "veredicto_razon": razon,
        "estadisticas": {
            "total_empleados": stats.get("empleados", 0),
            "total_conceptos": validacion.get("registros_por_tipo", {}).get("03", 0),
            "errores_criticos": len(criticos),
            "advertencias": len(advertencias),
        },
        "errores_arca": {
            "presente": bool(arca_contexto),
            "total": 0,
            "resumen": "",
        },
        "problemas": problemas,
    }


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


def _adjuntar_detalle_tecnico(informe: dict, validacion: dict) -> dict:
    issues = validacion.get("issues", [])
    agrupados: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        agrupados[issue.get("id", "")].append(issue)
    for problema in informe.get("problemas", []) or []:
        rule_id = problema.get("id")
        if rule_id and rule_id in agrupados:
            problema["detalle_tecnico"] = _detalle_tecnico_problema(rule_id, agrupados[rule_id])
    return informe


# ── Fase 1: Análisis ──────────────────────────────────────────────────────────

def ejecutar_analisis(session_id: str, q: queue.Queue):
    """Fase 1: validación determinística + explicación con Gemini."""
    sesion = SESIONES.get(session_id)
    if not sesion:
        q.put({"tipo": "error", "mensaje": "Sesión no encontrada."}); q.put(None); return

    ruta          = sesion['ruta']
    ruta_err      = sesion.get('ruta_errores')
    tiene_errores = ruta_err is not None
    modo          = sesion.get('modo', 'auto')

    q.put({"tipo": "herramienta", "nombre": "validacion_deterministica",
           "label": "Ejecutando validaciones determinísticas..."})
    try:
        deterministico = ejecutar_validaciones_deterministicas(ruta)
    except Exception as exc:
        q.put({"tipo": "error", "mensaje": f"Error validando TXT: {exc}"}); q.put(None); return

    arca_contexto = None
    if tiene_errores:
        try:
            from validador_errores import tool_parsear_errores_arca, tool_cruzar_errores_con_lsd
            q.put({"tipo": "herramienta", "nombre": "parsear_errores_arca",
                   "label": TOOL_LABELS.get("parsear_errores_arca", "Leyendo errores ARCA...")})
            errores_arca = tool_parsear_errores_arca(ruta_err)
            q.put({"tipo": "herramienta", "nombre": "cruzar_errores_con_lsd",
                   "label": TOOL_LABELS.get("cruzar_errores_con_lsd", "Cruzando errores ARCA...")})
            cruce_arca = tool_cruzar_errores_con_lsd(ruta_err, ruta)
            arca_contexto = {"errores_arca": errores_arca, "cruce_arca": cruce_arca}
        except Exception as exc:
            arca_contexto = {"error": str(exc)}

    sesion['validacion_deterministica'] = deterministico

    if modo == "rapido":
        informe = _informe_basico_deterministico(deterministico, arca_contexto)
        informe["modo_analisis"] = "rapido"
        sesion['informe'] = informe
        q.put({"tipo": "informe", "data": informe})
        q.put(None)
        return

    if modo == "auto" and deterministico.get("errores_criticos", 0) == 0 and deterministico.get("advertencias", 0) == 0 and not arca_contexto:
        informe = _informe_basico_deterministico(deterministico)
        informe["modo_analisis"] = "auto_sin_ia"
        sesion['informe'] = informe
        q.put({"tipo": "informe", "data": informe})
        q.put(None)
        return

    client = _get_client()
    if not client:
        informe = _informe_basico_deterministico(deterministico, arca_contexto)
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
        "del JSON determinístico y, si existe, el contexto de errores ARCA.\n\n"
        f"Archivo LSD: {os.path.abspath(ruta)}\n"
        f"Validación determinística:\n{json.dumps(deterministico, ensure_ascii=False, indent=2)}\n\n"
    )
    if arca_contexto:
        msg_texto += f"Contexto ARCA:\n{json.dumps(arca_contexto, ensure_ascii=False, indent=2)}\n\n"
    msg_texto += "Respondé ÚNICAMENTE con el JSON estructurado solicitado por el sistema."

    if pdf_parts:
        msg_texto += f"\n\nAdjunto {len(pdf_parts)} documentos de normativa LSD para enriquecer el diagnóstico."

    config = gtypes.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT_ANALISIS,
        temperature=0.1,
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
            informe = _adjuntar_detalle_tecnico(informe, deterministico)
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

    ruta_errores = None
    if 'archivo_errores' in request.files:
        f_err = request.files['archivo_errores']
        if f_err and f_err.filename and f_err.filename.lower().endswith('.txt'):
            with tempfile.NamedTemporaryFile(
                mode='wb', suffix='.txt', delete=False, prefix='err_arca_'
            ) as tmp_err:
                f_err.save(tmp_err)
                ruta_errores = tmp_err.name

    session_id = str(uuid.uuid4())
    SESIONES[session_id] = {
        'ruta':              ruta_tmp,
        'ruta_errores':      ruta_errores,
        'tiene_errores':     ruta_errores is not None,
        'modo':              modo,
        'informe':           None,
        'historial_analisis': []
    }

    def generar():
        q = queue.Queue()
        t = threading.Thread(target=ejecutar_analisis, args=(session_id, q), daemon=True)
        t.start()
        yield f"data: {json.dumps({'tipo': 'session', 'session_id': session_id, 'tiene_errores': ruta_errores is not None})}\n\n"
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
    for m in messages[-12:]:
        role = "model" if m.get("role") == "assistant" else "user"
        text = str(m.get("content") or "")
        if text.strip():
            contenido.append(gtypes.Part.from_text(text=f"{role.upper()}: {text}"))

    try:
        resp = client.models.generate_content(
            model=MODELO,
            contents=[gtypes.Content(role="user", parts=contenido)],
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
