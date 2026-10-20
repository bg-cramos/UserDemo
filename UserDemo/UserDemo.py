import json
import asyncio
import os
import re
import traceback
import subprocess
from datetime import datetime

from nats.aio.client import Client as NATS
import pyodbc


# ============================================================
# CONFIGURACION
# ============================================================

LOG_FILE = r"D:\Mailchimp\AltaUserDemo\NatsPython_Log.txt"

NATS_SERVER = "nats://localhost:4222"
NATS_SUBJECT = "globalcontactform"

EVENT_SOURCE = "Mailchimp"

HEARTBEAT_SECONDS = 300

IMAGEN_FILE = r"D:\Mailchimp\AltaUserDemo\imagen_base64.txt"


# ============================================================
# ESTADISTICAS
# ============================================================

mensajes_recibidos = 0
mensajes_procesados = 0
mensajes_error = 0

ultima_conexion = None
ultimo_mensaje = None
ultimo_error = None


# ============================================================
# LOG
# ============================================================

def escribir_log(nivel, mensaje):

    fecha = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    linea = f"{fecha} [{nivel}] {mensaje}"

    print(linea, flush=True)

    try:
        directorio = os.path.dirname(LOG_FILE)

        if directorio:
            os.makedirs(directorio, exist_ok=True)

        with open(LOG_FILE, "a", encoding="utf-8") as archivo:
            archivo.write(linea + "\n")
            archivo.flush()

    except Exception as e:
        print(
            f"{fecha} [ERROR] No se pudo escribir el log: {e}",
            flush=True
        )


def log_info(mensaje):
    escribir_log("INFO", mensaje)


def log_ok(mensaje):
    escribir_log("OK", mensaje)


def log_error(mensaje):
    escribir_log("ERROR", mensaje)


def log_warning(mensaje):
    escribir_log("WARNING", mensaje)


def log_heartbeat(mensaje):
    escribir_log("HEARTBEAT", mensaje)


# ============================================================
# EVENT VIEWER DE WINDOWS
# ============================================================

def evento_windows(tipo, event_id, mensaje):

    try:
        tipo_evento = {
            "INFO": "INFORMATION",
            "ERROR": "ERROR",
            "WARNING": "WARNING"
        }.get(tipo, "INFORMATION")

        mensaje_evento = str(mensaje).replace('"', "'")
        mensaje_evento = mensaje_evento.replace("\r", " ")
        mensaje_evento = mensaje_evento.replace("\n", " ")

        resultado = subprocess.run(
            [
                "eventcreate",
                "/L", "APPLICATION",
                "/T", tipo_evento,
                "/SO", EVENT_SOURCE,
                "/ID", str(event_id),
                "/D", mensaje_evento
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False
        )

        if resultado.returncode != 0:
            log_warning(
                "No se pudo registrar el evento de Windows: "
                + resultado.stderr.strip()
            )

    except Exception as e:
        log_warning(f"Error escribiendo en Event Viewer: {e}")


# ============================================================
# LOG + EVENT VIEWER
# ============================================================

def info(mensaje, event_id=None):

    log_info(mensaje)

    if event_id is not None:
        evento_windows("INFO", event_id, mensaje)


def ok(mensaje, event_id=None):

    log_ok(mensaje)

    if event_id is not None:
        evento_windows("INFO", event_id, mensaje)


def error(mensaje, event_id=None):

    log_error(mensaje)

    if event_id is not None:
        evento_windows("ERROR", event_id, mensaje)


def warning(mensaje, event_id=None):

    log_warning(mensaje)

    if event_id is not None:
        evento_windows("WARNING", event_id, mensaje)


# ============================================================
# GENERAR USUARIO
# ============================================================

def generar_usuario(fname, lname):

    fname = fname.strip().upper()
    lname = lname.strip().upper()

    if not fname:
        raise ValueError("El nombre no puede estar vacío")

    if not lname:
        raise ValueError("El apellido no puede estar vacío")

    return f"{fname[0]}{lname}"[:10]


# ============================================================
# RECONSTRUIR DATOS EN LA EMPRESA DEL GRUPO
# ============================================================

def reconstruir_datos_usuario(cursor, usuario):

    info("Buscando empresa asignada al grupo SUPERVISOR")

    cursor.execute(
        """
        SELECT Empresa_ID
        FROM INFORMAC_CONTACTFORMPBP.dbo.Info_Empresas
        WHERE Grupo_Id = ?
          AND Usuario_Id = ?
        """,
        "SUPERVISOR",
        "SUPERVISOR"
    )

    row = cursor.fetchone()

    if row is None or not row[0]:
        raise Exception(
            "No se encontró una empresa asignada "
            "al grupo SUPERVISOR en Info_Empresas"
        )

    empresa_id = str(row[0]).strip()

    info(f"Empresa asignada al grupo SUPERVISOR: {empresa_id}")

    # La base de datos es CONTACTFORMPBP,
    # no el nombre descriptivo de la empresa.
    base_datos = "CONTACTFORMPBP"

    info(f"Ejecutando reconstrucción en la base {base_datos}")

    cursor.execute(
        f"""
        EXEC [{base_datos}].[dbo].[Insertar_Datos_Usuarios_Reconstruccion]
            @Usuario = ?
        """,
        usuario.upper()
    )

    info(
        f"Reconstrucción completada para el usuario "
        f"'{usuario}' en la base '{base_datos}'"
    )


# ============================================================
# CREAR USUARIO
# ============================================================

def crear_usuario(
    dto_usuario,
    connection,
    use_transaction=True,
    user_name=""
):

    cursor = connection.cursor()

    try:

        if use_transaction:
            connection.autocommit = False

        # -----------------------------------------------------
        # VALIDAR EMAIL
        # -----------------------------------------------------

        info(
            f"Validando email para usuario "
            f"'{dto_usuario['Usuario']}'"
        )

        cursor.execute(
            """
            EXEC info_usuarios_preferencias_validateEmail
                @Email = ?,
                @Usuario = ?
            """,
            dto_usuario["IGGLOBALONLINE_USR_EMAIL"].upper(),
            dto_usuario["Usuario"].upper()
        )

        row = cursor.fetchone()

        if row is None:
            raise Exception(
                "El procedimiento de validación "
                "no devolvió resultados"
            )

        columns = [column[0] for column in cursor.description]

        if "check" not in columns:
            raise Exception(
                "El procedimiento de validación no devolvió "
                "la columna 'check'"
            )

        check_index = columns.index("check")
        check = row[check_index]

        # -----------------------------------------------------
        # CREAR USUARIO
        # -----------------------------------------------------

        if not check:

            info(f"Creando usuario '{dto_usuario['Usuario']}'")

            cursor.execute(
                """
                EXEC Info_Usuarios_insert
                    @Usuario_ID = ?,
                    @Nombre = ?,
                    @Clave = ?
                """,
                dto_usuario["Usuario"].upper(),
                dto_usuario["Nombre"],
                dto_usuario["Password"]
            )

            # -------------------------------------------------
            # INSERTAR PREFERENCIAS
            # -------------------------------------------------

            info("Insertando preferencias del usuario")

            cursor.execute(
                """
                EXEC Info_Usuarios_Preferencias_insert
                    @Usuario_ID = ?,
                    @Email = ?,
                    @UserImage = ?,
                    @Diccionario = ?,
                    @Nombre = ?,
                    @EmailDestinatario = ?
                """,
                dto_usuario["Usuario"],
                dto_usuario["IGGLOBALONLINE_USR_EMAIL"],
                dto_usuario["IGGLOBALONLINE_USR_FOTO_BASE64"],
                dto_usuario["IGGLOBALONLINE_I18N_DICCIONARIO"],
                dto_usuario["Nombre"],
                dto_usuario["IGGLOBALONLINE_USR_EMAIL_NOTIFICATION"]
            )

            # -------------------------------------------------
            # INSERTAR O ACTUALIZAR IMAGEN BASE64
            # -------------------------------------------------

            info("Insertando o actualizando imagen Base64")

            usuario_id = dto_usuario["Usuario"].upper()
            preferencia_id = "IGGLOBALONLINE_USR_FOTO_BASE64"
            imagen_base64 = dto_usuario[
                "IGGLOBALONLINE_USR_FOTO_BASE64"
            ]

            cursor.execute(
                """
                IF EXISTS (
                    SELECT 1
                    FROM dbo.Info_Usuarios_Preferencias
                    WHERE Usuario_ID = ?
                      AND Preferencia_ID = ?
                )
                BEGIN
                    UPDATE dbo.Info_Usuarios_Preferencias
                    SET Valor = ?
                    WHERE Usuario_ID = ?
                      AND Preferencia_ID = ?
                END
                ELSE
                BEGIN
                    INSERT INTO dbo.Info_Usuarios_Preferencias
                        (Usuario_ID, Preferencia_ID, Valor)
                    VALUES (?, ?, ?)
                END
                """,
                (
                    usuario_id,
                    preferencia_id,
                    imagen_base64,
                    usuario_id,
                    preferencia_id,
                    usuario_id,
                    preferencia_id,
                    imagen_base64
                )
            )

            info("Imagen Base64 insertada o actualizada correctamente")

            # -------------------------------------------------
            # ASIGNAR GRUPO
            # -------------------------------------------------

            info("Asignando grupo 01DEMO")

            cursor.execute(
                """
                EXEC info_pertenencias_single_insert
                    @User = ?,
                    @Grupo = '01DEMO'
                """,
                dto_usuario["Usuario"]
            )

            info("Grupo 01DEMO asignado")

            # -------------------------------------------------
            # RECONSTRUIR DATOS EN LA EMPRESA DEL GRUPO
            # -------------------------------------------------

            info(
                f"Reconstruyendo datos para el usuario "
                f"'{dto_usuario['Usuario']}'"
            )

            reconstruir_datos_usuario(
                cursor,
                dto_usuario["Usuario"]
            )

            # -------------------------------------------------
            # DEFAULTS
            # -------------------------------------------------

            value_default = dto_usuario.get("valueDefault", [])

            if value_default:
                info(
                    f"Insertando {len(value_default)} "
                    f"valores por defecto"
                )

            for value in value_default:

                solo_lectura = bool(
                    value.get("SoloLectura", False)
                )

                invisible = bool(
                    value.get("Invisible", False)
                )

                cursor.execute(
                    """
                    EXEC Info_Seg_Formularios_Sistema_Defaults_insert
                        @UsuarioId = ?,
                        @FormUrl = ?,
                        @Campo = ?,
                        @Valor = ?,
                        @SoloLectura = ?,
                        @Invisible = ?,
                        @_valorAsObject = ?
                    """,
                    dto_usuario["Usuario"].upper(),
                    value.get("FormUrl"),
                    value.get("Campo"),
                    value.get("Valor"),
                    solo_lectura,
                    invisible,
                    value.get("_valorAsObject")
                )

            # -------------------------------------------------
            # USUARIO DEL SISTEMA
            # -------------------------------------------------

            if not user_name:
                user_name = "SYSTEM"

        else:

            raise Exception(
                "El mail ingresado ya existe "
                "para otro usuario"
            )

        # -----------------------------------------------------
        # COMMIT
        # -----------------------------------------------------

        if use_transaction:
            connection.commit()

        ok(
            f"Usuario '{dto_usuario['Usuario']}' "
            f"creado correctamente",
            200
        )

        return True

    except Exception:

        if use_transaction:
            try:
                connection.rollback()
            except Exception:
                pass

        raise

    finally:
        cursor.close()


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def message_handler(msg):

    global mensajes_recibidos
    global mensajes_procesados
    global mensajes_error
    global ultimo_mensaje
    global ultimo_error

    connection = None

    mensajes_recibidos += 1
    ultimo_mensaje = datetime.now()

    try:

        # -----------------------------------------------------
        # BODY
        # -----------------------------------------------------

        body = msg.data.decode("utf-8")

        info("========================================")
        info(f"Mensaje NATS recibido (#{mensajes_recibidos})", 210)
        info(f"Subject: {msg.subject}")
        info(f"Tamaño del mensaje: {len(msg.data)} bytes")
        info(f"Contenido recibido: {body}")

        # -----------------------------------------------------
        # JSON
        # -----------------------------------------------------

        try:
            data = json.loads(body)

        except json.JSONDecodeError as e:

            mensajes_error += 1
            ultimo_error = datetime.now()

            error(f"El mensaje recibido no es un JSON válido: {e}", 220)
            error(f"Contenido inválido: {body}")

            return

        info(f"JSON recibido correctamente: {data}")

        # -----------------------------------------------------
        # VALIDAR CAMPOS
        # -----------------------------------------------------

        campos_requeridos = ["FNAME", "LNAME", "EMAIL"]

        for campo in campos_requeridos:

            if campo not in data:
                raise KeyError(campo)

            if data[campo] is None:
                raise ValueError(
                    f"El campo '{campo}' no puede ser NULL"
                )

            if not isinstance(data[campo], str):
                raise ValueError(
                    f"El campo '{campo}' debe ser texto"
                )

        # -----------------------------------------------------
        # SQL SERVER
        # -----------------------------------------------------

        info("Conectando a SQL Server", 230)

        connection = pyodbc.connect(
            "DRIVER={ODBC Driver 17 for SQL Server};"
            "SERVER=IG-CLOUD;"
            "DATABASE=INFORMAC_CONTACTFORMPBP;"
            "UID=sa;"
            "PWD=+base$5317;"
        )

        ok("Conexión SQL Server establecida", 231)

        # -----------------------------------------------------
        # DTO
        # -----------------------------------------------------

        usuario_generado = generar_usuario(
            data["FNAME"],
            data["LNAME"]
        )

        info(f"Usuario generado: {usuario_generado}")

        with open(IMAGEN_FILE, "r", encoding="utf-8") as archivo:
            imagen_base64 = archivo.read().strip()

        if not imagen_base64:
            raise ValueError("El archivo de imagen Base64 está vacío")

        dto_usuario = {
            "Usuario": usuario_generado,
            "Nombre": data["FNAME"] + " " + data["LNAME"],
            "Password": "password321+",
            "IGGLOBALONLINE_USR_EMAIL": usuario_generado + "@global.com.ar",
            "IGGLOBALONLINE_USR_FOTO_BASE64": imagen_base64,
            "IGGLOBALONLINE_I18N_DICCIONARIO": "1",
            "IGGLOBALONLINE_USR_EMAIL_NOTIFICATION": data["EMAIL"],
            "seleccionGrupos": [
                {"Grupo_Id": "01DEMO"}
            ],
            "valueDefault": []
        }

        # -----------------------------------------------------
        # CREAR USUARIO
        # -----------------------------------------------------

        crear_usuario(
            dto_usuario=dto_usuario,
            connection=connection,
            use_transaction=True,
            user_name="SYSTEM"
        )

        mensajes_procesados += 1

        ok(
            f"Procesamiento del mensaje terminado correctamente "
            f"(procesados={mensajes_procesados})",
            240
        )

    except KeyError as e:

        mensajes_error += 1
        ultimo_error = datetime.now()

        error(f"Falta un campo obligatorio en el JSON: {e}", 221)
        error(traceback.format_exc())

    except Exception as e:

        mensajes_error += 1
        ultimo_error = datetime.now()

        error(f"Error procesando mensaje: {e}", 222)
        error(traceback.format_exc())

    finally:

        # -----------------------------------------------------
        # CERRAR SQL
        # -----------------------------------------------------

        if connection is not None:

            try:
                connection.close()
                info("Conexión SQL cerrada")

            except Exception as e:
                warning(f"Error cerrando conexión SQL: {e}")


# ============================================================
# HEARTBEAT
# ============================================================

async def heartbeat(nc):

    while True:

        try:

            await asyncio.sleep(HEARTBEAT_SECONDS)

            estado_nats = (
                "CONNECTED"
                if nc.is_connected
                else "DISCONNECTED"
            )

            ultimo_mensaje_texto = (
                ultimo_mensaje.strftime("%d/%m/%Y %H:%M:%S")
                if ultimo_mensaje
                else "NINGUNO"
            )

            ultimo_error_texto = (
                ultimo_error.strftime("%d/%m/%Y %H:%M:%S")
                if ultimo_error
                else "NINGUNO"
            )

            log_heartbeat(
                "Servicio activo | "
                f"NATS={estado_nats} | "
                f"Recibidos={mensajes_recibidos} | "
                f"Procesados={mensajes_procesados} | "
                f"Errores={mensajes_error} | "
                f"UltimoMensaje={ultimo_mensaje_texto} | "
                f"UltimoError={ultimo_error_texto}"
            )

        except asyncio.CancelledError:
            break

        except Exception as e:
            error(f"Error en heartbeat: {e}")


# ============================================================
# MAIN
# ============================================================

async def main():

    global ultima_conexion

    info("========================================")
    info("NATS PYTHON - INICIO", 100)
    info(f"Python ejecutando desde: {os.sys.executable}")
    info(f"Archivo de log: {LOG_FILE}")
    info(f"Servidor NATS: {NATS_SERVER}")
    info(f"Subject NATS: {NATS_SUBJECT}")

    nc = NATS()

    # ---------------------------------------------------------
    # CALLBACKS NATS
    # ---------------------------------------------------------

    async def disconnected_cb():
        warning("NATS desconectado", 300)

    async def reconnected_cb():

        global ultima_conexion

        ultima_conexion = datetime.now()

        ok(
            f"NATS reconectado correctamente a "
            f"{nc.connected_url.netloc}",
            301
        )

    async def error_cb(e):
        error(f"Error NATS: {e}", 302)

    async def closed_cb():
        warning("Conexión NATS cerrada", 303)

    # ---------------------------------------------------------
    # CONECTAR NATS
    # ---------------------------------------------------------

    info(f"Conectando a NATS {NATS_SERVER}", 101)

    await nc.connect(
        NATS_SERVER,
        disconnected_cb=disconnected_cb,
        reconnected_cb=reconnected_cb,
        error_cb=error_cb,
        closed_cb=closed_cb
    )

    ultima_conexion = datetime.now()

    info(
        f"NATS conectado a {nc.connected_url.netloc}",
        102
    )

    # ---------------------------------------------------------
    # SUSCRIPCION
    # ---------------------------------------------------------

    sub = await nc.subscribe(
        NATS_SUBJECT,
        cb=message_handler
    )

    ok(
        f"Suscripto a subject '{NATS_SUBJECT}' (sid={sub._id})",
        103
    )

    # ---------------------------------------------------------
    # HEARTBEAT
    # ---------------------------------------------------------

    heartbeat_task = asyncio.create_task(heartbeat(nc))

    info("Esperando mensajes...", 104)
    info("Servicio NATS iniciado correctamente.")

    # ---------------------------------------------------------
    # MANTENER PROCESO
    # ---------------------------------------------------------

    try:

        while True:
            await asyncio.sleep(1)

    finally:

        info("Cerrando servicio NATS...")

        heartbeat_task.cancel()

        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        if nc.is_connected:
            info("Cerrando conexión NATS...")
            await nc.close()

        info("Conexión NATS cerrada")
        info("NATS PYTHON - FIN")


# ============================================================
# ARRANQUE
# ============================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        info("Proceso detenido manualmente")

    except Exception as e:
        error(f"ERROR FATAL: {e}", 500)
        error(traceback.format_exc())
        raise