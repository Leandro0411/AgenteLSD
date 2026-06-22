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
from flask import Flask, render_template, request, Response, jsonify, session

sys.path.insert(0, os.path.dirname(__file__))
from agente_lsd import (
    TOOL_DECLARATIONS, TOOL_FUNCTIONS,
    TOOL_DECLARATIONS_ALL, TOOL_FUNCTIONS_ALL,
    SYSTEM_PROMPT, MODELO,
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
    "validar_conteo_reg04_en_reg01":  "Controlando conteo de empleados declarado...",
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

IMPORTANTE — PRECHECKS YA EJECUTADOS:
Antes de que leas este mensaje, se ejecutaron automáticamente 14 validaciones
hardcoded sobre el archivo. Los resultados te llegan como salidas de herramientas.
TU ROL ES:
  1. Interpretar y explicar en lenguaje simple los resultados de esos prechecks.
  2. Detectar errores de NEGOCIO que el código no puede verificar:
     - Bases imponibles inconsistentes con la normativa MOPRE vigente
     - Detracción (Base 9 / Base 10) mal calculada
     - Diferencias en cruce con el archivo de errores ARCA (si se subió)
     - Cualquier otro problema semántico que requiera leer el contenido
  3. NO repitas análisis que los prechecks ya hicieron (duplicados, longitudes,
     comas, conteos, CBU, SAC fuera de período, etc.).
  4. Si un precheck encontró un error, incorporalo en "problemas" con su tutorial.
  5. Si todos los prechecks reportaron "ok: true", enfocate en los errores de negocio.

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


# ── Fase 1: Análisis ──────────────────────────────────────────────────────────

def ejecutar_analisis(session_id: str, q: queue.Queue):
    """Fase 1: análisis completo del archivo con Gemini."""
    sesion = SESIONES.get(session_id)
    if not sesion:
        q.put({"tipo": "error", "mensaje": "Sesión no encontrada."}); q.put(None); return

    client = _get_client()
    if not client:
        q.put({"tipo": "error", "mensaje": "GEMINI_API_KEY no configurada."}); q.put(None); return

    ruta          = sesion['ruta']
    ruta_err      = sesion.get('ruta_errores')
    tiene_errores = ruta_err is not None

    # PDFs de normativa (opcionales)
    pdf_parts = _cargar_pdf_parts()

    # Mensaje inicial
    if tiene_errores:
        msg_texto = (
            f"Analizá el archivo LSD y los errores de validación ARCA. "
            f"Producí el informe en formato JSON.\n\n"
            f"Archivo LSD: {os.path.abspath(ruta)}\n"
            f"Archivo de errores ARCA: {os.path.abspath(ruta_err)}\n\n"
            "Orden sugerido:\n"
            "  1. info_archivo (LSD)\n"
            "  2. parsear_errores_arca (errores ARCA)\n"
            "  3. cruzar_errores_con_lsd (cruce LSD + errores)\n"
            "  4. validar_estructura, validar_duplicados_reg04, validar_comas_numericos, "
            "validar_bases_reg04, validar_cbu, analizar_conceptos_reg03\n\n"
            "Respondé ÚNICAMENTE con el JSON estructurado."
        )
    else:
        msg_texto = (
            f"Analizá el archivo LSD y producí el informe en formato JSON.\n\n"
            f"Archivo: {os.path.abspath(ruta)}\n\n"
            "Ejecutá TODAS las herramientas de validación. "
            "Respondé ÚNICAMENTE con el JSON estructurado."
        )

    if pdf_parts:
        msg_texto += f"\n\nAdjunto {len(pdf_parts)} documentos de normativa LSD para enriquecer el diagnóstico."

    # Herramientas activas según contexto
    decls_activas = TOOL_DECLARATIONS_ALL if tiene_errores else TOOL_DECLARATIONS
    fns_activas   = TOOL_FUNCTIONS_ALL    if tiene_errores else TOOL_FUNCTIONS

    config = gtypes.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT_ANALISIS,
        tools=[gtypes.Tool(function_declarations=decls_activas)],
        temperature=0.1,
    )

    # Historial: texto + PDFs opcionales en el primer mensaje
    historial: list[gtypes.Content] = [
        gtypes.Content(
            role="user",
            parts=pdf_parts + [gtypes.Part.from_text(text=msg_texto)]
        )
    ]

    paso = 0
    while True:
        paso += 1
        if paso > 25:
            q.put({"tipo": "error", "mensaje": "Límite de análisis superado."}); break

        # Llamar a Gemini con retry para errores transitorios (503, 429, 500)
        resp = None
        for intento in range(4):
            try:
                resp = client.models.generate_content(
                    model=MODELO,
                    contents=historial,
                    config=config,
                )
                break  # éxito
            except Exception as e:
                msg = str(e)
                es_transitorio = any(c in msg for c in ('503', '429', '500', 'UNAVAILABLE', 'overloaded'))
                if es_transitorio and intento < 3:
                    espera = 5 * (2 ** intento)  # 5s, 10s, 20s
                    q.put({"tipo": "aviso", "mensaje": f"Gemini ocupado, reintentando en {espera}s... (intento {intento+2}/4)"})
                    time.sleep(espera)
                else:
                    q.put({"tipo": "error", "mensaje": msg}); q.put(None); return


        # Agregar respuesta al historial
        historial.append(resp.candidates[0].content)

        # IMPORTANTE: En Gemini, finish_reason=STOP tanto para tool calls
        # como para respuestas finales. Hay que chequear fn_calls PRIMERO.
        fn_calls = _extraer_fn_calls(resp)

        if fn_calls:
            # El modelo quiere llamar herramientas → procesarlas
            pass  # continúa al bloque de fn_responses abajo
        elif _es_fin(resp):
            # No hay tool calls y el modelo terminó → extraer respuesta final
            texto = _extraer_texto_respuesta(resp)
            if texto:
                # Limpiar posibles markdown fences
                if texto.startswith("```"):
                    partes = texto.split("```")
                    texto = partes[1] if len(partes) > 1 else texto
                    if texto.startswith("json"):
                        texto = texto[4:]
                try:
                    informe = json.loads(texto.strip())
                    sesion['informe'] = informe
                    sesion['historial_analisis'] = historial
                    q.put({"tipo": "informe", "data": informe})
                except json.JSONDecodeError:
                    q.put({"tipo": "texto_libre", "data": texto})
            else:
                q.put({"tipo": "error", "mensaje": "El modelo no devolvió respuesta. Intentá de nuevo."})
            break
        else:
            # Sin tool calls y sin finish → algo raro, salir
            break

        if not fn_calls:
            break

        fn_responses = []
        for fc in fn_calls:
            fn_name  = fc.name
            fn_input = dict(fc.args)

            q.put({"tipo": "herramienta", "nombre": fn_name,
                   "label": TOOL_LABELS.get(fn_name, fn_name)})

            if fn_name in fns_activas:
                try:
                    res = fns_activas[fn_name](**fn_input)
                except Exception as exc:
                    res = {"ok": False, "error": str(exc)}
            else:
                res = {"ok": False, "error": f"Herramienta desconocida: {fn_name}"}

            fn_responses.append(
                gtypes.Part.from_function_response(name=fn_name, response=res)
            )

        historial.append(gtypes.Content(role="user", parts=fn_responses))

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