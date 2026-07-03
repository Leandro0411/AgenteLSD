import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash

DB_NAME = "users.db"

def conectar_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def inicializar_base_datos():
    """Crea la tabla de usuarios si no existe en SQLite."""
    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL,
        rol      TEXT NOT NULL DEFAULT 'usuario',
        creado   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()

def crear_usuario(username, password, rol='usuario'):
    """Registra un usuario encriptando la contraseña."""
    inicializar_base_datos()
    conn = conectar_db()
    cursor = conn.cursor()
    # Genera un hash seguro (PBKDF2) para no guardar texto plano
    password_hash = generate_password_hash(password)
    try:
        cursor.execute(
            "INSERT INTO users (username, password, rol) VALUES (?, ?, ?)",
            (username.strip().lower(), password_hash, rol)
        )
        conn.commit()
        return True, f"Usuario '{username}' creado exitosamente como '{rol}'."
    except sqlite3.IntegrityError:
        return False, "Error: El nombre de usuario ya existe."
    finally:
        conn.close()

def verificar_usuario(username, password):
    """Valida las credenciales de un intento de login."""
    inicializar_base_datos()
    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username.strip().lower(),))
    user = cursor.fetchone()
    conn.close()
    
    # Compara el hash guardado contra la contraseña ingresada
    if user and check_password_hash(user['password'], password):
        return dict(user)
    return None

def listar_usuarios():
    """Devuelve la lista de cuentas registradas."""
    inicializar_base_datos()
    conn = conectar_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, rol, creado FROM users")
    usuarios = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return usuarios

def cambiar_password(username, nueva_password):
    """Cambia la clave de un usuario existente."""
    inicializar_base_datos()
    conn = conectar_db()
    cursor = conn.cursor()
    password_hash = generate_password_hash(nueva_password)
    cursor.execute("UPDATE users SET password = ? WHERE username = ?", (password_hash, username.strip().lower()))
    conn.commit()
    rows_affected = cursor.rowcount
    conn.close()
    return rows_affected > 0