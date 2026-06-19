#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
correcciones.py — Herramientas de corrección automática del TXT LSD
Cada función recibe la ruta del archivo, aplica la corrección y devuelve
un resumen de lo que cambió. El archivo original se preserva con sufijo .bak
"""

import os
import re
import shutil
from collections import defaultdict

# ── Constantes (mismas que agente_lsd.py) ───────────────────────────────────
TIPO_START = 0;  TIPO_END   = 2
CUIL_START = 2;  CUIL_END   = 13
REG03_COD_START = 13; REG03_COD_END = 20
REG04_BASE4_START  = 220; REG04_BASE4_END    = 235
REG04_BASE10_START = 355; REG04_BASE10_END   = 370


def _leer_lineas(ruta: str) -> tuple[list[str], str]:
    """Lee el archivo y devuelve (lineas, encoding_usado)."""
    for enc in ('utf-8', 'latin-1', 'cp1252'):
        try:
            with open(ruta, encoding=enc) as f:
                return [l.rstrip('\r\n') for l in f], enc
        except UnicodeDecodeError:
            continue
    raise ValueError(f"No se pudo decodificar {ruta}")


def _guardar(ruta: str, lineas: list[str], enc: str) -> None:
    """Guarda el archivo preservando el original como .bak"""
    bak = ruta + '.bak'
    if not os.path.exists(bak):
        shutil.copy2(ruta, bak)
    with open(ruta, 'w', encoding=enc, newline='\r\n') as f:
        for l in lineas:
            f.write(l + '\r\n')


def _tipo(l: str) -> str:
    return l[TIPO_START:TIPO_END] if len(l) >= TIPO_END else '??'


def _cuil(l: str) -> str:
    return l[CUIL_START:CUIL_END] if len(l) >= CUIL_END else '?'


# ── BUG A: REG04 duplicado ───────────────────────────────────────────────────

def tool_corregir_duplicados_reg04(ruta: str) -> dict:
    """
    Corrige el Bug A: consolida múltiples REG04 del mismo CUIL en uno solo.
    Estrategia: para cada campo numérico de 15 chars, SUMA los valores de
    todos los REG04 del mismo CUIL. Conserva la primera cabecera (CUIL, legajo,
    CBU, etc.) y descarta las líneas extra.
    Guarda .bak antes de modificar.
    """
    try:
        lineas, enc = _leer_lineas(ruta)

        # Recolectar REG04 por CUIL
        reg04_por_cuil: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for i, l in enumerate(lineas):
            if l.strip() and _tipo(l) == '04':
                reg04_por_cuil[_cuil(l)].append((i, l))

        duplicados = {c: v for c, v in reg04_por_cuil.items() if len(v) > 1}
        if not duplicados:
            return {"ok": True, "corregidos": 0, "mensaje": "No había duplicados para corregir."}

        lineas_a_eliminar = set()
        cambios = []

        for cuil, ocurrencias in duplicados.items():
            idx_primero, linea_base = ocurrencias[0]
            # Acumular campos numéricos de 15 chars desde pos 13 en adelante
            # Trabajamos con la línea más larga como base
            base = list(linea_base)
            max_len = max(len(l) for _, l in ocurrencias)
            # Padding si hace falta
            while len(base) < max_len:
                base.append(' ')

            for idx_extra, linea_extra in ocurrencias[1:]:
                # Recorrer bloques de 15 chars desde pos 13
                pos = 13
                while pos + 15 <= min(len(linea_base), len(linea_extra)):
                    seg_base  = linea_base[pos:pos+15].strip()
                    seg_extra = linea_extra[pos:pos+15].strip()
                    try:
                        val = float(seg_base or '0') + float(seg_extra or '0')
                        nuevo = f"{val:015.2f}".replace('.', '')  # centavos implícitos
                        # Mantener formato original: si ambos son enteros, no poner punto
                        if '.' not in seg_base and '.' not in seg_extra:
                            nuevo = str(int(val)).zfill(15)
                        else:
                            nuevo = f"{val:.2f}".zfill(15)
                            if len(nuevo) > 15:
                                nuevo = nuevo[:15]
                        base[pos:pos+15] = list(nuevo.ljust(15))
                    except ValueError:
                        pass  # campo no numérico, dejar base
                    pos += 15

                lineas_a_eliminar.add(idx_extra)
                cambios.append(f"CUIL {cuil}: eliminada línea {idx_extra+1}, bases sumadas a línea {idx_primero+1}")

            lineas[idx_primero] = ''.join(base)

        nuevas_lineas = [l for i, l in enumerate(lineas) if i not in lineas_a_eliminar]
        _guardar(ruta, nuevas_lineas, enc)

        return {
            "ok": True,
            "cuils_corregidos": len(duplicados),
            "lineas_eliminadas": len(lineas_a_eliminar),
            "cambios": cambios[:20],
            "mensaje": (
                f"Se consolidaron {len(duplicados)} empleados con REG04 duplicado. "
                f"Se eliminaron {len(lineas_a_eliminar)} líneas duplicadas. "
                f"El archivo original fue guardado como {ruta}.bak"
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── BUG B: Comas en campos numéricos ────────────────────────────────────────

def tool_corregir_comas_numericos(ruta: str) -> dict:
    """
    Corrige el Bug B (#30999): reemplaza comas por puntos en campos numéricos
    de REG03 y REG04 (posición 13 en adelante).
    Convierte "1.000,50" → "1000.50" eliminando también el punto de miles.
    Guarda .bak antes de modificar.
    """
    try:
        lineas, enc = _leer_lineas(ruta)
        nuevas = []
        corregidos = 0
        cambios = []

        for i, l in enumerate(lineas):
            if not l.strip() or _tipo(l) not in ('03', '04'):
                nuevas.append(l)
                continue

            encabezado = l[:13]
            cuerpo     = l[13:]

            if ',' not in cuerpo:
                nuevas.append(l)
                continue

            # Convertir formato argentino: quitar puntos de miles, cambiar coma decimal por punto
            def normalizar(m):
                s = m.group(0)
                # Patrón: dígitos con puntos de miles y coma decimal: "1.234,56"
                if re.match(r'^\d{1,3}(\.\d{3})*,\d+$', s):
                    return s.replace('.', '').replace(',', '.')
                # Solo coma sin punto de miles: "1234,56"
                if re.match(r'^\d+,\d+$', s):
                    return s.replace(',', '.')
                return s

            cuerpo_nuevo = re.sub(r'\d[\d.,]*\d|\d', normalizar, cuerpo)

            if cuerpo_nuevo != cuerpo:
                corregidos += 1
                cambios.append({
                    "linea": i + 1,
                    "cuil": _cuil(l),
                    "tipo": _tipo(l),
                    "antes": cuerpo[:60],
                    "despues": cuerpo_nuevo[:60]
                })

            nuevas.append(encabezado + cuerpo_nuevo)

        if corregidos == 0:
            return {"ok": True, "corregidos": 0, "mensaje": "No había comas para corregir."}

        _guardar(ruta, nuevas, enc)
        return {
            "ok": True,
            "registros_corregidos": corregidos,
            "cambios": cambios[:20],
            "mensaje": (
                f"Se corrigieron {corregidos} registros con comas en campos numéricos. "
                f"El archivo original fue guardado como {ruta}.bak"
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── BUG C: Concepto 560k → 570k en REG03 ────────────────────────────────────

def tool_corregir_concepto_560_a_570(ruta: str) -> dict:
    """
    Corrige el Bug C (Guía N°45): reemplaza el código ARCA '5600000' por '5700000'
    en todos los registros REG03 afectados.
    IMPORTANTE: Esta corrección solo actualiza el código en REG03.
    Las bases imponibles en REG04 (Base4, Base8, Base10) requieren recalcular
    desde e-SUELDOS porque dependen de los aportes OS del empleado.
    Guarda .bak antes de modificar.
    """
    try:
        lineas, enc = _leer_lineas(ruta)
        nuevas = []
        corregidos = 0
        cuils_afectados = set()
        cambios = []

        for i, l in enumerate(lineas):
            if not l.strip() or _tipo(l) != '03':
                nuevas.append(l)
                continue

            if len(l) < REG03_COD_END:
                nuevas.append(l)
                continue

            cod = l[REG03_COD_START:REG03_COD_END]
            if cod.strip() == '5600000':
                nueva_linea = l[:REG03_COD_START] + '5700000' + l[REG03_COD_END:]
                nuevas.append(nueva_linea)
                corregidos += 1
                cuil = _cuil(l)
                cuils_afectados.add(cuil)
                cambios.append({
                    "linea": i + 1,
                    "cuil": cuil,
                    "antes": "5600000",
                    "despues": "5700000"
                })
            else:
                nuevas.append(l)

        if corregidos == 0:
            return {"ok": True, "corregidos": 0, "mensaje": "No se encontró el concepto 560.000 para corregir."}

        _guardar(ruta, nuevas, enc)
        return {
            "ok": True,
            "registros_corregidos": corregidos,
            "cuils_afectados": len(cuils_afectados),
            "cambios": cambios[:20],
            "advertencia": (
                "⚠️  El código fue actualizado a 570.000 en REG03. "
                "Sin embargo, las bases imponibles en REG04 (aportes OS, Base4, Base8, Base10) "
                "todavía tienen los valores del concepto viejo. "
                "Para que el archivo quede 100% correcto, regenerá el LSD desde e-SUELDOS "
                "después de configurar el concepto 570.000 en el sistema."
            ),
            "mensaje": (
                f"Se actualizaron {corregidos} líneas REG03 de {len(cuils_afectados)} empleados: "
                f"concepto 560.000 → 570.000. "
                f"El archivo original fue guardado como {ruta}.bak"
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Definición de herramientas para la API ───────────────────────────────────

TOOLS_CORRECCION = [
    {
        "name": "corregir_duplicados_reg04",
        "description": (
            "Corrige el Bug A: cuando un empleado tiene dos legajos activos y aparece "
            "dos veces en el archivo (REG04 duplicado), consolida ambos registros en uno "
            "sumando los campos numéricos. Modifica el archivo en disco. "
            "Usar solo después de que el usuario confirmó la corrección."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ruta": {"type": "string", "description": "Ruta al archivo TXT de LSD a corregir"}
            },
            "required": ["ruta"]
        }
    },
    {
        "name": "corregir_comas_numericos",
        "description": (
            "Corrige el Bug B (#30999): reemplaza comas por puntos en campos numéricos "
            "de REG03 y REG04, convirtiendo formato argentino (1.000,50) al formato ARCA (1000.50). "
            "Modifica el archivo en disco. "
            "Usar solo después de que el usuario confirmó la corrección."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ruta": {"type": "string", "description": "Ruta al archivo TXT de LSD a corregir"}
            },
            "required": ["ruta"]
        }
    },
    {
        "name": "corregir_concepto_560_a_570",
        "description": (
            "Corrige el Bug C (Guía N°45): reemplaza el código de concepto ARCA 560.000 "
            "por 570.000 en todos los REG03 afectados. "
            "Modifica el archivo en disco. "
            "ADVERTENCIA: las bases imponibles en REG04 quedan desactualizadas y requieren "
            "regenerar desde e-SUELDOS. Usar solo después de que el usuario confirmó."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ruta": {"type": "string", "description": "Ruta al archivo TXT de LSD a corregir"}
            },
            "required": ["ruta"]
        }
    }
]

TOOL_FUNCTIONS_CORRECCION = {
    "corregir_duplicados_reg04":   tool_corregir_duplicados_reg04,
    "corregir_comas_numericos":    tool_corregir_comas_numericos,
    "corregir_concepto_560_a_570": tool_corregir_concepto_560_a_570,
    "aplicar_cambios_propuestos":  None,   # se maneja directamente en app.py
}


# ── Corrección general: aplica cambios por línea/posición ────────────────────

def tool_aplicar_cambios_propuestos(ruta: str, cambios: list) -> dict:
    """
    Aplica una lista de cambios a posiciones exactas del archivo TXT.
    cambios = [
        {
          "linea":       int,   # 1-indexed
          "campo_inicio": int,  # 0-indexed, inclusivo
          "campo_fin":    int,  # 0-indexed, exclusivo
          "valor_nuevo":  str,  # valor a escribir (se trunca/padea al ancho exacto)
          "descripcion":  str   # texto legible del cambio
        }, ...
    ]
    """
    try:
        lineas, enc = _leer_lineas(ruta)
        aplicados   = []
        errores     = []

        for c in cambios:
            num_linea   = int(c.get("linea", 0))
            inicio      = int(c.get("campo_inicio", 0))
            fin         = int(c.get("campo_fin", 0))
            valor_nuevo = str(c.get("valor_nuevo", ""))
            descripcion = c.get("descripcion", "")
            ancho       = fin - inicio

            if num_linea < 1 or num_linea > len(lineas):
                errores.append(f"Línea {num_linea} fuera de rango (el archivo tiene {len(lineas)} líneas)")
                continue

            idx  = num_linea - 1
            linea = lineas[idx]

            # Extender la línea si es más corta que la posición requerida
            if len(linea) < fin:
                linea = linea.ljust(fin)

            valor_aplicar = valor_nuevo[:ancho].ljust(ancho)  # truncar o padear con espacios
            valor_anterior = linea[inicio:fin]

            lineas[idx] = linea[:inicio] + valor_aplicar + linea[fin:]
            aplicados.append({
                "linea":      num_linea,
                "descripcion": descripcion,
                "antes":      repr(valor_anterior),
                "despues":    repr(valor_aplicar),
            })

        if not aplicados:
            return {"ok": False, "error": "Ningún cambio pudo aplicarse. " + "; ".join(errores)}

        _guardar(ruta, lineas, enc)

        msg_errores = f" ({len(errores)} cambios no pudieron aplicarse)" if errores else ""
        return {
            "ok":       True,
            "aplicados": len(aplicados),
            "cambios":  aplicados[:30],
            "mensaje":  (
                f"Se aplicaron {len(aplicados)} cambio(s) al archivo{msg_errores}. "
                f"El archivo original fue guardado como {ruta}.bak"
            )
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}