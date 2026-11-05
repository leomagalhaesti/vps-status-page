"""
Página de status de servidor no estilo Easypanel (somente leitura).

GET  /status            página (pede a senha)
POST /status/login      confere a senha e guarda um cookie por 30 dias
GET  /status/api        tudo o que a página mostra (cookie)
GET  /status/api/logs   últimas linhas de um container ou serviço (?alvo=nome)

Roda no host (systemd, porta 127.0.0.1:8097) atrás de um proxy reverso (Traefik).
Coleta os dados num laço a cada 20 s, então abrir a página não pesa no servidor.
Não reinicia nem apaga nada.

Configuração: projetos.json (ver projetos.exemplo.json) e a variável STATUS_SENHA.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

AQUI = os.path.dirname(os.path.abspath(__file__))
PORTA = 8097


SENHA = os.environ.get("STATUS_SENHA", "")
COOKIE = hmac.new(SENHA.encode(), b"status", hashlib.sha256).hexdigest()

# Projetos como no Easypanel: cada um junta containers (c:), serviços do host (s:) e sites (w:).
_CFG = json.load(open(os.path.join(AQUI, "projetos.json")))
PROJETOS, NOMES = _CFG["projetos"], _CFG.get("nomes", {})

ESTADO = {"pronto": False}
_cpu_antes = None


def sh(*cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa
        return ""


def cpu_pct():
    global _cpu_antes
    v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    ocioso, total = v[3] + v[4], sum(v)
    pct = None
    if _cpu_antes:
        dt = total - _cpu_antes[1]
        pct = round(100 * (1 - (ocioso - _cpu_antes[0]) / dt), 1) if dt else 0
    _cpu_antes = (ocioso, total)
    return pct


def sistema():
    mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
    disco = shutil.disk_usage("/")
    return {"cpu": cpu_pct(), "nucleos": os.cpu_count(), "carga": open("/proc/loadavg").read().split()[:3],
            "ram_total": mem["MemTotal"] * 1024, "ram_usada": (mem["MemTotal"] - mem["MemAvailable"]) * 1024,
            "swap_total": mem["SwapTotal"] * 1024, "swap_usada": (mem["SwapTotal"] - mem["SwapFree"]) * 1024,
            "disco_total": disco.total, "disco_usado": disco.used,
            "ligado": float(open("/proc/uptime").read().split()[0])}


def containers():
    out = {}
    for l in sh("docker", "ps", "-a", "--format", "{{json .}}").splitlines():
        d = json.loads(l)
        out[d["Names"]] = {"estado": d["State"], "texto": d["Status"], "imagem": d["Image"]}
    for l in sh("docker", "stats", "--no-stream", "--format", "{{json .}}").splitlines():
        d = json.loads(l)
        if d["Name"] in out:
            out[d["Name"]].update(cpu=d["CPUPerc"], mem=d["MemUsage"].split(" / ")[0])
    if out:
        for d in json.loads(sh("docker", "inspect", *out) or "[]"):
            nome = d["Name"].lstrip("/")
            st = d["State"]
            out[nome].update(reinicios=d.get("RestartCount", 0), desde=st.get("StartedAt", ""),
                             oom=st.get("OOMKilled", False), saida=st.get("ExitCode", 0),
                             saude=(st.get("Health") or {}).get("Status", ""))
    return out


def site(url):
    t = time.time()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "status-page"}), timeout=8)
        return {"codigo": r.status, "ms": round((time.time() - t) * 1000)}
    except urllib.error.HTTPError as e:
        return {"codigo": e.code, "ms": round((time.time() - t) * 1000)}
    except Exception as e:  # noqa
        return {"codigo": 0, "ms": None, "erro": str(e)[:120]}










def coletar():
    cs = containers()
    projetos, problemas = [], []
    for p in PROJETOS:
        itens = []
        for item in p["itens"]:
            tipo, nome = item.split(":", 1)
            if tipo == "c":
                c = cs.get(nome)
                ok = bool(c) and c["estado"] == "running" and c.get("saude") in ("", "healthy")
                itens.append({"tipo": "app", "id": nome, "nome": NOMES.get(nome, nome), "ok": ok,
                              "aviso": bool(c) and c.get("saude") == "starting", **(c or {"estado": "não existe"})})
            elif tipo == "s":
                st = sh("systemctl", "is-active", nome).strip() or "?"
                ts = sh("systemctl", "show", nome, "-p", "ActiveEnterTimestamp", "--value", "--timestamp=unix").strip().lstrip("@")
                desde = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts))) if ts.isdigit() and int(ts) else ""
                itens.append({"tipo": "serviço", "id": nome, "nome": NOMES.get(nome, nome), "ok": st == "active",
                              "estado": st, "desde": desde})
            else:
                r = site(nome)
                itens.append({"tipo": "site", "id": nome, "nome": urlparse(nome).hostname, "ok": 200 <= r["codigo"] < 400, **r})
        for i in itens:
            if not i["ok"]:
                problemas.append({"nivel": "erro", "texto": f'{p["nome"]}: {i["nome"]} está {i.get("estado") or "fora do ar"}'})
        proj = {"id": p["id"], "nome": p["nome"], "cor": p["cor"], "itens": itens}
        projetos.append(proj)
    s = sistema()
    if s["disco_usado"] / s["disco_total"] > 0.85:
        problemas.append({"nivel": "erro", "texto": "Disco acima de 85%"})
    if s["ram_usada"] / s["ram_total"] > 0.9:
        problemas.append({"nivel": "aviso", "texto": "Memória acima de 90%"})
    for nome, c in cs.items():
        if c.get("oom"):
            problemas.append({"nivel": "aviso", "texto": f"{NOMES.get(nome, nome)} já foi morto por falta de memória"})
    ESTADO.update(pronto=True, quando=time.time(), sistema=s, projetos=projetos, problemas=problemas)




def laco():
    cpu_pct()
    while True:
        try:
            coletar()
        except Exception as e:  # noqa
            ESTADO["erro_coleta"] = str(e)[:200]
        time.sleep(20)


ALVOS_LOG = {i.split(":", 1)[1]: i[0] for p in PROJETOS for i in p["itens"] if i[0] in "cs"}


def logs(alvo):
    tipo = ALVOS_LOG.get(alvo)
    if tipo == "c":
        r = subprocess.run(["docker", "logs", "--tail", "80", "--timestamps", alvo], capture_output=True, text=True, timeout=15)
        txt = r.stdout + r.stderr
    elif tipo == "s":
        txt = sh("journalctl", "-u", alvo, "-n", "80", "--no-pager", "-o", "short-iso")
    else:
        return None
    return txt[-20000:]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def logado(self):
        return hmac.compare_digest(f"st={COOKIE}", next((c.strip() for c in self.headers.get("Cookie", "").split(";")
                                                          if c.strip().startswith("st=")), ""))

    def manda(self, codigo, corpo, tipo="application/json", extra=()):
        b = corpo.encode() if isinstance(corpo, str) else corpo
        self.send_response(codigo)
        self.send_header("Content-Type", tipo + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/status", "/status/"):
            return self.manda(200, open(os.path.join(AQUI, "pagina.html"), encoding="utf-8").read(), "text/html")
        if not self.logado():
            return self.manda(401, '{"erro":"senha"}')
        if u.path == "/status/api":
            return self.manda(200, json.dumps(ESTADO, ensure_ascii=False))
        if u.path == "/status/api/logs":
            txt = logs(parse_qs(u.query).get("alvo", [""])[0])
            return self.manda(404, '{"erro":"alvo"}') if txt is None else self.manda(200, json.dumps({"txt": txt}, ensure_ascii=False))
        self.manda(404, '{"erro":"rota"}')

    def do_POST(self):
        if urlparse(self.path).path != "/status/login":
            return self.manda(404, '{"erro":"rota"}')
        try:
            senha = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 2000)) or b"{}").get("senha", "")
        except Exception:  # noqa
            senha = ""
        if not (SENHA and hmac.compare_digest(str(senha).encode(), SENHA.encode())):
            time.sleep(1)  # atrasa tentativas de adivinhar a senha
            return self.manda(401, '{"erro":"senha"}')
        self.manda(200, '{"ok":true}', extra=[("Set-Cookie", f"st={COOKIE}; Path=/status; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax")])


if __name__ == "__main__":
    threading.Thread(target=laco, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORTA), H).serve_forever()
