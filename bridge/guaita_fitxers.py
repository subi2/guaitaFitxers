#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Guaita-Fitxers · pont local v0.8.1 · SBS BIM Consulting 2026
Navegador local de fitxers RVT (.rvt .rfa .rte .rft)
- Versió de RVT (BasicFileInfo)
- Historial: primer i darrer editor (Global/DocumentIncrementTable)
- Miniatura (RevitPreview4.0): visualització, descàrrega i substitució
- Obrir amb la versió de RVT instal·lada que es triï

Ús:      python guaita_fitxers.py [carpeta_inicial]
Requisits: pip install olefile          (lectura)
Opcional:  pip install pillow pywin32   (edició de miniatura, només Windows)
"""

import base64, glob, io, json, os, re, string, struct, subprocess, sys, threading
import urllib.parse, zlib, shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import olefile
except ImportError:
    sys.exit("Cal el paquet 'olefile':  pip install olefile")

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

IS_WIN = os.name == "nt"
EXTS = (".rvt", ".rfa", ".rte", ".rft")
PORT = 8765
# Orígens web permesos (només l'app publicada i l'accés local). Qualsevol altre origen es rebutja.
ALLOWED_ORIGINS = {"https://subi2.github.io", "http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}
MAX_BODY = 20 * 1024 * 1024  # límit de la petició (bytes)
PNG_SIG = b"\x89PNG\r\n\x1a\n"

def is_rvt(path):
    """Només s'accepten fitxers RVT: evita llegir, obrir o modificar altres fitxers."""
    return bool(path) and os.path.normpath(path).lower().endswith(EXTS)

# ──────────────────────────────── Parsing RVT ────────────────────────────────

def _u32(b, o): return struct.unpack_from("<I", b, o)[0]

def _find_u16(data, text):
    """Cerca 'text' codificat UTF-16LE dins bytes; retorna offset o -1 (a prova de desalineació)."""
    return data.find(text.encode("utf-16-le"))

def parse_basicfileinfo(raw):
    """Extreu versió i usuari del stream BasicFileInfo (cerca byte-level, no depèn de l'offset 687)."""
    info = {"version": None, "build": None, "username": None}
    # Format: 2025   (RVT ≥2021 aprox.)
    m = _find_u16(raw, "Format:")
    if m >= 0:
        tail = raw[m:m + 80].decode("utf-16-le", "ignore")
        g = re.search(r"Format:\s*(\d{4})", tail)
        if g: info["version"] = g.group(1)
    # Fallback: "Autodesk Revit 2019" dins RVT Build:
    if not info["version"]:
        m = _find_u16(raw, "Autodesk Revit")
        if m >= 0:
            tail = raw[m:m + 120].decode("utf-16-le", "ignore")
            g = re.search(r"Autodesk Revit(?:\s+\w+)*\s+(20\d\d)", tail)
            if g: info["version"] = g.group(1)
    m = _find_u16(raw, "Build:")
    if m >= 0:
        tail = raw[m:m + 160].decode("utf-16-le", "ignore")
        g = re.search(r"Build:[ \t]*([^\r\n\x00]+)", tail)
        if g: info["build"] = g.group(1).strip()
    m = _find_u16(raw, "Username:")
    if m >= 0:
        tail = raw[m:m + 160].decode("utf-16-le", "ignore")
        g = re.search(r"Username:[ \t]*([^\r\n\x00]+)", tail)
        if g: info["username"] = g.group(1).strip()
    return info

def _gunzip_tolerant(raw):
    """Descomprimeix gzip saltant la capçalera de 8 bytes de RVT, tolerant amb padding final."""
    for skip in (8, 0):
        try:
            d = zlib.decompressobj(16 + 15)
            out = d.decompress(raw[skip:])
            out += d.flush()
            if out: return out
        except zlib.error:
            continue
    return None

def _record_at(data, pos):
    """Intenta llegir un registre d'increment a pos. Retorna (usuari, ts, inc, nova_pos) o None."""
    if pos + 4 > len(data): return None
    n = _u32(data, pos)
    if not (0 < n <= 128): return None
    end_name = pos + 4 + n * 2
    if end_name + 2 + 16 + 8 > len(data): return None
    name_b = data[pos + 4:end_name]
    if any(name_b[i] not in (0,) and name_b[i] > 0x02 for i in range(1, len(name_b), 2)):
        return None  # bytes alts no plausibles per a noms llatins
    try:
        name = name_b.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not name or any(c < " " for c in name): return None
    p = end_name + 2 + 16
    ts = _u32(data, p)
    inc = _u32(data, p + 4)
    if not (946684800 < ts < 4102444800):  # 2000–2100
        return None
    return (name, ts, inc, p + 8)

def parse_increment_table(raw):
    """Retorna la llista de desats [(usuari, timestamp_unix, increment)] o []."""
    data = _gunzip_tolerant(raw)
    if not data: return []
    recs, pos = [], 0
    # resincronització inicial per si hi ha capçalera abans del primer registre
    start = next((o for o in range(0, min(64, len(data))) if _record_at(data, o)), None)
    if start is None: return []
    pos = start
    while True:
        r = _record_at(data, pos)
        if not r:
            # resync suau dins d'un marge curt
            nxt = next((o for o in range(pos + 1, min(pos + 32, len(data))) if _record_at(data, o)), None)
            if nxt is None: break
            pos = nxt
            continue
        name, ts, inc, pos = r
        recs.append((name, ts, inc))
    return recs

def extract_png_span(raw):
    """Localitza el PNG dins el stream de preview. Retorna (ini, fi) del PNG o None."""
    i = raw.find(PNG_SIG)
    if i < 0: return None
    p = i + 8
    while p + 8 <= len(raw):
        ln = struct.unpack_from(">I", raw, p)[0]
        typ = raw[p + 4:p + 8]
        p += 8 + ln + 4
        if typ == b"IEND":
            return (i, p)
    return None

def read_rvt_info(path):
    """Llegeix metadades d'un fitxer RVT. Retorna dict."""
    out = {"version": None, "build": None, "first_by": None, "first_ts": None,
           "last_by": None, "last_ts": None, "saves": 0, "has_thumb": False, "error": None}
    try:
        ole = olefile.OleFileIO(path)
    except Exception as e:
        out["error"] = f"No és un fitxer OLE2 vàlid ({e})"
        return out
    try:
        if ole.exists("BasicFileInfo"):
            bfi = parse_basicfileinfo(ole.openstream("BasicFileInfo").read())
            out.update({k: bfi[k] for k in ("version", "build")})
            out["username"] = bfi["username"]
        for name in (["Global", "DocumentIncrementTable"], ["DocumentIncrementTable"]):
            if ole.exists(name):
                recs = parse_increment_table(ole.openstream(name).read())
                if recs:
                    out["saves"] = len(recs)
                    out["first_by"], out["first_ts"], _ = recs[0]
                    out["last_by"], out["last_ts"], _ = recs[-1]
                break
        for name in ("RevitPreview4.0", ["RevitPreview", "Preview"]):
            if ole.exists(name):
                out["has_thumb"] = True
                out["_thumb_stream"] = name if isinstance(name, str) else "/".join(name)
                break
    except Exception as e:
        out["error"] = str(e)
    finally:
        ole.close()
    return out

def read_thumbnail(path):
    """Retorna els bytes PNG de la miniatura o None."""
    try:
        ole = olefile.OleFileIO(path)
    except Exception:
        return None
    try:
        for name in ("RevitPreview4.0", ["RevitPreview", "Preview"]):
            if ole.exists(name):
                raw = ole.openstream(name).read()
                span = extract_png_span(raw)
                if span: return raw[span[0]:span[1]]
        return None
    finally:
        ole.close()

# ─────────────────────────── Escriptura de miniatura ─────────────────────────

def write_thumbnail(path, img_bytes):
    """Substitueix el PNG dins RevitPreview4.0 (només Windows, via Structured Storage).
    Fa còpia .bak abans. Retorna (ok, missatge)."""
    if not IS_WIN:
        return False, "L'edició de miniatures només és possible a Windows (Structured Storage natiu)."
    try:
        import pythoncom
        from win32com import storagecon as sc
    except ImportError:
        return False, "Cal pywin32:  pip install pywin32"

    # llegeix stream actual amb olefile per conèixer el span PNG
    try:
        ole = olefile.OleFileIO(path)
        stream_name = None
        for name in ("RevitPreview4.0",):
            if ole.exists(name):
                stream_name = name
                raw = ole.openstream(name).read()
        ole.close()
    except Exception as e:
        return False, f"No es pot llegir el fitxer: {e}"
    if not stream_name:
        return False, "El fitxer no té stream RevitPreview4.0."
    span = extract_png_span(raw)
    if not span:
        return False, "No s'ha trobat cap PNG dins la miniatura existent."
    ini, fi = span
    old_png = raw[ini:fi]

    # normalitza la imatge nova a PNG amb les mateixes dimensions que l'antiga
    new_png = _normalize_png(img_bytes, old_png)
    if new_png is None:
        return False, "La imatge nova no és un PNG vàlid (instal·la 'pillow' per convertir JPG/altres)."

    prefix, suffix = raw[:ini], raw[fi:]
    # si el prefix conté la longitud de l'antic PNG com a uint32 LE, actualitza-la
    old_len_le = struct.pack("<I", len(old_png))
    if prefix.count(old_len_le) == 1:
        prefix = prefix.replace(old_len_le, struct.pack("<I", len(new_png)))
    new_raw = prefix + new_png + suffix

    shutil.copy2(path, path + ".bak")
    try:
        flags = sc.STGM_READWRITE | sc.STGM_SHARE_EXCLUSIVE | sc.STGM_TRANSACTED
        stg = pythoncom.StgOpenStorage(path, None, flags, None, 0)
        stm = stg.OpenStream(stream_name, None, sc.STGM_READWRITE | sc.STGM_SHARE_EXCLUSIVE, 0)
        stm.SetSize(len(new_raw))
        stm.Seek(0, 0)
        stm.Write(new_raw)
        stg.Commit(sc.STGC_DEFAULT)
        stm = None; stg = None
        return True, "Miniatura substituïda. S'ha creat una còpia de seguretat .bak."
    except Exception as e:
        return False, f"Error escrivint (còpia .bak conservada): {e}"

def _normalize_png(img_bytes, old_png):
    """Converteix img_bytes a PNG amb les dimensions del PNG antic. Sense Pillow, accepta només PNG tal qual."""
    if HAS_PIL:
        try:
            ow, oh = struct.unpack(">II", old_png[16:24])
            im = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
            im = im.resize((ow, oh), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()
        except Exception:
            return None
    return img_bytes if img_bytes[:8] == PNG_SIG else None

# ────────────────────────────── RVT instal·lats ────────────────────────────

def installed_rvts():
    """Detecta els Revit.exe instal·lats. Retorna [{year, exe}] ordenat."""
    if not IS_WIN: return []
    found = []
    for pf in (os.environ.get("ProgramFiles", r"C:\Program Files"),
               os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        for exe in glob.glob(os.path.join(pf, "Autodesk", "Revit *", "Revit.exe")):
            m = re.search(r"Revit\s+(20\d\d)", exe)
            if m: found.append({"year": m.group(1), "exe": exe})
    found.sort(key=lambda r: r["year"])
    return found

RVTS = installed_rvts()
START_DIR = os.path.expanduser("~")

# ─────────────────────────────── Servidor HTTP ───────────────────────────────

def list_dir(path):
    path = os.path.abspath(path)
    dirs, files = [], []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_dir():
                    if not e.name.startswith((".", "$")):
                        dirs.append(e.name)
                elif e.name.lower().endswith(EXTS):
                    st = e.stat()
                    files.append({"name": e.name, "size": st.st_size, "mtime": int(st.st_mtime)})
            except OSError:
                continue
    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())
    drives = []
    if IS_WIN:
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    parent = os.path.dirname(path.rstrip("\\/")) or None
    if parent == path: parent = None
    return {"path": path, "parent": parent, "dirs": dirs, "files": files, "drives": drives}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _origin_ok(self):
        """Peticions sense Origin (eines locals) o des d'un origen permès."""
        o = self.headers.get("Origin")
        return o is None or o in ALLOWED_ORIGINS

    def _cors(self):
        o = self.headers.get("Origin")
        if o in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _forbidden(self):
        body = json.dumps({"error": "origen no permès"}).encode()
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not self._origin_ok():
            return self._forbidden()
        self.send_response(204)
        self._cors()
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype, code=200):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._origin_ok():
            return self._forbidden()
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = (q.get("path") or [""])[0]
        try:
            if u.path == "/":
                self._bytes("Guaita-Fitxers · pont local v0.8.1".encode(), "text/plain; charset=utf-8")
            elif u.path == "/api/browse":
                self._json(list_dir(p or START_DIR))
            elif u.path == "/api/info":
                if not is_rvt(p): return self._json({"error": "No és un fitxer RVT"}, 400)
                info = read_rvt_info(p)
                info.pop("_thumb_stream", None)
                self._json(info)
            elif u.path == "/api/thumb":
                if not is_rvt(p): return self._json({"error": "No és un fitxer RVT"}, 400)
                png = read_thumbnail(p)
                if png: self._bytes(png, "image/png")
                else: self._json({"error": "sense miniatura"}, 404)
            elif u.path == "/api/rvts":
                self._json(RVTS)
            else:
                self._json({"error": "ruta desconeguda"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        if not self._origin_ok():
            return self._forbidden()
        u = urllib.parse.urlparse(self.path)
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return self._json({"ok": False, "msg": "Cal Content-Type application/json"}, 415)
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            return self._json({"ok": False, "msg": "Petició massa gran"}, 413)
        body = self.rfile.read(length)
        try:
            if u.path == "/api/reveal":
                d = json.loads(body)
                path = os.path.normpath(d.get("path", ""))
                if not is_rvt(path) or not os.path.isfile(path):
                    return self._json({"ok": False, "msg": "Fitxer RVT no vàlid"}, 400)
                if IS_WIN:
                    subprocess.Popen(["explorer", "/select," + path], close_fds=True)
                else:
                    subprocess.Popen(["xdg-open", os.path.dirname(path)], close_fds=True)
                self._json({"ok": True, "msg": "Obrint l'Explorer…"})
            elif u.path == "/api/open":
                d = json.loads(body)
                exe = d.get("exe"); path = d.get("path")
                if not any(r["exe"] == exe for r in RVTS):
                    return self._json({"ok": False, "msg": "Executable no reconegut"}, 400)
                if not is_rvt(path) or not os.path.isfile(path):
                    return self._json({"ok": False, "msg": "Fitxer RVT no vàlid"}, 400)
                subprocess.Popen([exe, path], close_fds=True)
                self._json({"ok": True, "msg": f"Obrint amb {os.path.basename(os.path.dirname(exe))}…"})
            elif u.path == "/api/thumb":
                d = json.loads(body)
                if not is_rvt(d.get("path", "")) or not os.path.isfile(d.get("path", "")):
                    return self._json({"ok": False, "msg": "Fitxer RVT no vàlid"}, 400)
                img = base64.b64decode(d["data"])
                ok, msg = write_thumbnail(d["path"], img)
                self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
            else:
                self._json({"error": "ruta desconeguda"}, 404)
        except Exception as e:
            self._json({"ok": False, "msg": str(e)}, 500)

# ──────────────────────────────────── Main ───────────────────────────────────

if __name__ == "__main__":
    START_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.expanduser("~")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Guaita-Fitxers (pont) · {url}\nCarpeta inicial: {START_DIR}\nOrígens permesos: {', '.join(sorted(ALLOWED_ORIGINS))}")
    if RVTS:
        print("RVT detectats:", ", ".join(r["year"] for r in RVTS))
    else:
        print("Cap RVT detectat (o no és Windows): el botó «Obre» no estarà disponible.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
