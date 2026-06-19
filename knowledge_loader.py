#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
knowledge_loader.py — Sube los PDFs de normativa LSD a Gemini Files API
========================================================================
Corré SOLO UNA VEZ (o cuando agregás PDFs nuevos).
Guarda los file refs en gemini_files.json para que agente_lsd.py los reutilice.

Gemini Files API mantiene los archivos activos por 48hs.
Si corrés el agente después de ese tiempo, volvé a correr este script.

USO:
    python knowledge_loader.py              # sube ./pdfs/*.pdf
    python knowledge_loader.py /otra/ruta  # sube desde otra carpeta

REQUISITOS:
    pip install google-genai
    Variable de entorno: GEMINI_API_KEY=AIza...
"""

import os
import sys
import json
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

REFS_FILE = "gemini_files.json"   # dónde se guardan los file refs
PDF_DIR   = "./pdfs"              # carpeta por defecto con los PDFs


def subir_pdfs(carpeta: str = PDF_DIR) -> list[dict]:
    """
    Sube todos los PDFs de la carpeta a Gemini Files API.
    Retorna lista de dicts con name, uri y display_name.
    """
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: Variable GEMINI_API_KEY no configurada.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    pdfs   = sorted(Path(carpeta).glob("*.pdf"))

    if not pdfs:
        print(f"No se encontraron PDFs en '{carpeta}'")
        sys.exit(1)

    print(f"\nSubiendo {len(pdfs)} PDFs a Gemini Files API...\n")
    refs = []

    for pdf_path in pdfs:
        print(f"  📄 {pdf_path.name} ({pdf_path.stat().st_size // 1024} KB)...")
        try:
            with open(pdf_path, "rb") as f:
                uploaded = client.files.upload(
                    file=f,
                    config={"mime_type": "application/pdf",
                            "display_name": pdf_path.stem}
                )

            # Esperar a que el archivo esté activo (estado ACTIVE)
            intentos = 0
            while uploaded.state.name == "PROCESSING" and intentos < 20:
                time.sleep(2)
                uploaded = client.files.get(name=uploaded.name)
                intentos += 1

            if uploaded.state.name != "ACTIVE":
                print(f"    ⚠  Estado inesperado: {uploaded.state.name}, saltando.")
                continue

            refs.append({
                "name":         uploaded.name,
                "uri":          uploaded.uri,
                "display_name": pdf_path.stem,
                "filename":     pdf_path.name,
            })
            print(f"    ✓ Subido: {uploaded.name}")

        except Exception as e:
            print(f"    ✗ Error subiendo {pdf_path.name}: {e}")

    # Guardar refs en disco
    with open(REFS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "subido_en": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total":     len(refs),
            "archivos":  refs,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n✅ {len(refs)} archivos subidos.")
    print(f"   Refs guardados en: {REFS_FILE}")
    print(f"   ⚠  Los archivos expiran en 48hs. Volvé a correr este script si pasa ese tiempo.\n")

    return refs


def cargar_refs() -> list[dict]:
    """
    Carga los file refs guardados en gemini_files.json.
    Retorna lista vacía si el archivo no existe (el agente funciona igual, sin PDFs).
    """
    if not os.path.exists(REFS_FILE):
        return []
    with open(REFS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("archivos", [])


if __name__ == "__main__":
    carpeta = sys.argv[1] if len(sys.argv) > 1 else PDF_DIR
    subir_pdfs(carpeta)