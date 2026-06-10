#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — Interfaz web para el Agente LSD
Flask + Server-Sent Events para streaming en tiempo real
"""

import os
import sys
import json
import tempfile
import threading
import queue
import time

# Cargar variables de entorno desde .env si existe
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv no instalado; usar variables de entorno del sistema

from flask import Flask, render_template, request, Response, jsonify
import anthropic
from collections import defaultdict

# Importar todo del agente original
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agente_lsd import (
    TOOLS, TOOL_FUNCTIONS, SYSTEM_PROMPT, MODELO,
    tool_info_archivo, tool_validar_estructura,
    tool_validar_duplicados_reg04, tool_validar_comas_numericos,
    tool_validar_bases_reg04, tool_validar_cbu,
    tool_analizar_conceptos_reg03
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

# Labels amigables para cada herramienta
TOOL_LABELS = {
    "info_archivo":             "Leyendo el archivo...",
    "validar_estructura":       "Verificando estructura del archivo...",
    "validar_duplicados_reg04": "Buscando empleados con legajos duplicados...",
    "validar_comas_numericos":  "Revisando formato de números...",
    "validar_bases_reg04":      "Verificando bases imponibles...",
    "validar_cbu":              "Controlando CBUs...",
    "analizar_conceptos_reg03": "Analizando conceptos declarados...",
}

SYSTEM_PROMPT_WEB = SYSTEM_PROMPT + """

═══════════════════════════════════════════════════════════════
INSTRUCCIONES DE FORMATO PARA INTERFAZ WEB
═══════════════════════════════════════════════════════════════
El informe final debe estar en formato JSON estricto, sin texto fuera del JSON.
Estructura exacta:

{
  "resumen": "2-3 oraciones en lenguaje simple explicando el estado general del archivo",
  "veredicto": "PRESENTABLE" | "SERÁ RECHAZADO" | "REVISAR",
  "veredicto_razon": "Una oración explicando el veredicto",
  "estadisticas": {
    "total_empleados": N,
    "total_conceptos": N,
    "errores_criticos": N,
    "advertencias": N
  },
  "problemas": [
    {
      "severidad": "CRITICO" | "ADVERTENCIA" | "INFO",
      "titulo": "Título corto en lenguaje simple (máximo 8 palabras)",
      "descripcion": "Qué está pasando, en lenguaje para un administrativo, sin jerga técnica",
      "cuils_afectados": N,
      "ejemplos_cuil": ["XX-XXXXXXXX-X", ...],
      "causa": "Por qué ocurre esto (1-2 oraciones simples)",
      "solucion": "Qué hay que hacer para corregirlo (pasos concretos, sin tecnicismos)"
    }
  ]
}

Si no hay problemas, "problemas" debe ser una lista vacía.
Respondé ÚNICAMENTE con el JSON, sin markdown, sin texto antes ni después.
"""


def ejecutar_agente_streaming(ruta: str, q: queue.Queue):
    """Ejecuta el agente y pone eventos en la queue para SSE."""

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        q.put({"tipo": "error", "mensaje": "API Key no configurada. Creá el archivo .env con ANTHROPIC_API_KEY=sk-ant-..."})
        q.put(None)
        return

    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        q.put({"tipo": "error", "mensaje": f"Error al conectar con la API: {str(e)}"})
        q.put(None)
        return

    messages = [
        {
            "role": "user",
            "content": (
                f"Analizá el siguiente archivo LSD y producí el informe en formato JSON.\n\n"
                f"Archivo: {os.path.abspath(ruta)}\n\n"
                "Ejecutá TODAS las herramientas disponibles antes de responder. "
                "Respondé ÚNICAMENTE con el JSON estructurado indicado en las instrucciones."
            )
        }
    ]

    paso = 0
    while True:
        paso += 1
        if paso > 25:
            q.put({"tipo": "error", "mensaje": "Se superó el límite de análisis. Intentá de nuevo."})
            break

        try:
            response = client.messages.create(
                model=MODELO,
                max_tokens=8192,
                system=SYSTEM_PROMPT_WEB,
                tools=TOOLS,
                messages=messages
            )
        except anthropic.APIStatusError as e:
            q.put({"tipo": "error", "mensaje": f"Error de API ({e.status_code}): {str(e)}"})
            break
        except Exception as e:
            q.put({"tipo": "error", "mensaje": f"Error inesperado: {str(e)}"})
            break

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, 'text') and block.text.strip():
                    texto = block.text.strip()
                    # Limpiar si viene con markdown
                    if texto.startswith("```"):
                        texto = texto.split("```")[1]
                        if texto.startswith("json"):
                            texto = texto[4:]
                    try:
                        informe = json.loads(texto)
                        q.put({"tipo": "informe", "data": informe})
                    except json.JSONDecodeError:
                        q.put({"tipo": "texto_libre", "data": texto})
            break

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            break

        tool_results = []
        for tu in tool_uses:
            fn_name = tu.name
            fn_input = tu.input

            label = TOOL_LABELS.get(fn_name, f"Ejecutando {fn_name}...")
            q.put({"tipo": "herramienta", "nombre": fn_name, "label": label})

            if fn_name in TOOL_FUNCTIONS:
                try:
                    result = TOOL_FUNCTIONS[fn_name](**fn_input)
                except Exception as exc:
                    result = {"ok": False, "error": f"Error en {fn_name}: {exc}"}
            else:
                result = {"ok": False, "error": f"Herramienta desconocida: {fn_name}"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result, ensure_ascii=False, default=str)
            })

        messages.append({"role": "user", "content": tool_results})

    q.put(None)  # Señal de fin


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analizar', methods=['POST'])
def analizar():
    if 'archivo' not in request.files:
        return jsonify({"error": "No se recibió ningún archivo"}), 400

    archivo = request.files['archivo']
    if archivo.filename == '':
        return jsonify({"error": "Nombre de archivo vacío"}), 400

    if not archivo.filename.lower().endswith('.txt'):
        return jsonify({"error": "El archivo debe ser un TXT"}), 400

    # Guardar temporalmente
    with tempfile.NamedTemporaryFile(
        mode='wb', suffix='.txt', delete=False, prefix='lsd_'
    ) as tmp:
        archivo.save(tmp)
        ruta_tmp = tmp.name

    def generar():
        q = queue.Queue()
        hilo = threading.Thread(
            target=ejecutar_agente_streaming,
            args=(ruta_tmp, q),
            daemon=True
        )
        hilo.start()

        try:
            while True:
                try:
                    evento = q.get(timeout=120)
                except queue.Empty:
                    yield f"data: {json.dumps({'tipo': 'error', 'mensaje': 'Tiempo de espera agotado'})}\n\n"
                    break

                if evento is None:
                    yield f"data: {json.dumps({'tipo': 'fin'})}\n\n"
                    break

                yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
        finally:
            try:
                os.unlink(ruta_tmp)
            except Exception:
                pass

    return Response(generar(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/health')
def health():
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    return jsonify({
        "ok": True,
        "api_configurada": bool(api_key),
        "modelo": MODELO
    })


if __name__ == '__main__':
    print()
    print("=" * 55)
    print("  AGENTE LSD — Interfaz Web")
    print("=" * 55)

    if not os.environ.get('ANTHROPIC_API_KEY'):
        print()
        print("  ⚠  ANTHROPIC_API_KEY no está configurada.")
        print("     Creá el archivo .env en esta carpeta con:")
        print("     ANTHROPIC_API_KEY=sk-ant-...")
        print()
    else:
        print()
        print("  ✓  API Key cargada correctamente.")
        print()

    print("  Abrí tu navegador en:  http://localhost:5000")
    print("=" * 55)
    print()
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)