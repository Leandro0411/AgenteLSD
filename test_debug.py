#!/usr/bin/env python3
import os, sys, queue, json, threading
sys.path.insert(0, os.path.dirname(__file__))
from app import ejecutar_analisis, SESIONES

ruta = 'Txts/librodigital_xthche.txt'
sid  = 'test-debug2'
SESIONES[sid] = {
    'ruta': ruta,
    'ruta_errores': None,
    'tiene_errores': False,
    'informe': None,
    'historial_analisis': []
}

q = queue.Queue()
t = threading.Thread(target=ejecutar_analisis, args=(sid, q), daemon=True)
t.start()

print('Esperando eventos SSE...')
while True:
    ev = q.get(timeout=180)
    if ev is None:
        print('\nFIN OK')
        break
    tipo = ev.get('tipo', '?')
    if tipo == 'herramienta':
        print(f'  [TOOL] {ev["nombre"]}')
    elif tipo == 'error':
        print(f'  [ERROR] {ev["mensaje"]}')
    elif tipo == 'informe':
        d = ev['data']
        print(f'  [INFORME] veredicto={d.get("veredicto","?")} errores_criticos={d.get("estadisticas",{}).get("errores_criticos","?")}')
        print(f'           problemas={len(d.get("problemas",[]))}')
    elif tipo == 'texto_libre':
        print(f'  [TEXTO] {str(ev["data"])[:120]}')
    else:
        print(f'  [EVT] {tipo}')
