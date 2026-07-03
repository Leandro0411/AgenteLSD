#!/usr/bin/env python3
import argparse
import sys
from auth import crear_usuario, listar_usuarios, cambiar_password, inicializar_base_datos

def main():
    parser = argparse.ArgumentParser(description="CLI de gestión de usuarios para Agente LSD")
    
    parser.add_argument("--init", action="store_true", help="Inicializa la base de datos")
    parser.add_argument("--crear", action="store_true", help="Crea un nuevo usuario")
    parser.add_argument("--listar", action="store_true", help="Lista todos los usuarios")
    parser.add_argument("--cambiar-pass", action="store_true", help="Cambia la contraseña de un usuario")
    
    parser.add_argument("--usuario", type=str, help="Nombre de usuario")
    parser.add_argument("--password", type=str, help="Contraseña del usuario")
    parser.add_argument("--nueva-pass", type=str, help="Nueva contraseña para el usuario")
    parser.add_argument("--rol", type=str, choices=["admin", "usuario"], default="usuario", help="Rol del usuario (admin o usuario)")
    
    args = parser.parse_args()
    
    if args.init:
        inicializar_base_datos()
        print("Base de datos 'users.db' inicializada correctamente.")
        return

    if args.crear:
        if not args.usuario or not args.password:
            print("Error: Se requiere --usuario y --password para crear una cuenta.")
            sys.exit(1)
        ok, msg = crear_usuario(args.usuario, args.password, args.rol)
        print(msg)
        return

    if args.listar:
        usuarios = listar_usuarios()
        if not usuarios:
            print("No hay usuarios registrados aún.")
            return
        print(f"\n{'ID':<4} | {'Usuario':<15} | {'Rol':<10} | {'Creado (UTC)':<20}")
        print("-" * 58)
        for u in usuarios:
            print(f"{u['id']:<4} | {u['username']:<15} | {u['rol']:<10} | {u['creado']:<20}")
        print()
        return

    if args.cambiar_pass:
        if not args.usuario or not args.nueva_pass:
            print("Error: Se requiere --usuario y --nueva-pass para cambiar la contraseña.")
            sys.exit(1)
        ok = cambiar_password(args.usuario, args.nueva_pass)
        if ok:
            print(f"Contraseña de '{args.usuario}' cambiada exitosamente.")
        else:
            print(f"Error: No se encontró al usuario '{args.usuario}'.")
        return

    parser.print_help()

if __name__ == "__main__":
    main()