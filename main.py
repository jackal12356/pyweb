#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enzonic 节点 main.py  —— 教材版（替换掉 eooce 的 sb 一键脚本）
==============================================================
这份脚本做三件事，全部用标准库，零第三方依赖：
  1) 在面板分配的端口上起一个小 HTTP 服务（/ 和 /sub），让容器保持 "running"；
  2) 下载官方 nezha-agent，改名后常驻运行，连你自己的哪吒面板上线；
  3) 起 bridge.py，容器主动 WSS 连你自己的 Cloudflare Worker(enz.9943826.xyz)，
     本地处理 VLESS —— 代替 argo/cloudflared，出站流量看起来就是普通 HTTPS。

== 相比原 eooce sb 脚本收敛了哪些暴露点（教材重点）==
  - 删掉了把 `ps aux` 丢给公网的 /ps 路由（原脚本最大的裸奔点）；
  - 不再从 amd64.eooce.com 拉 sbsh 黑盒二进制，全部自己可控；
  - nezha-agent 二进制改名成普通名字（见 NEZHA_RUN），ps 里不叫 nezha-agent；
  - 所有密钥走环境变量，命令行参数 /proc/<pid>/cmdline 不泄露；
  - 不跑 cloudflared，出站没有到 *.argotunnel.com 的指纹长连接。

配置都可用环境变量覆盖（面板 Startup/Variables 里设更干净）。
启动命令：python3 main.py
"""
import os
import sys
import time
import stat
import zipfile
import threading
import subprocess
import urllib.request
import http.server
import socketserver

# ---------------- 配置（环境变量优先） ----------------
PORT = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or 3000)
SUB_PATH = os.environ.get("SUB_PATH", "sub")            # 订阅路径 token

UUID      = os.environ.get("UUID", "")
NAME      = os.environ.get("NAME", "deplexo")
CFIP      = os.environ.get("CFIP", "saas.sin.fan")       # 订阅里客户端优选连接域名

# —— 你自己的 Cloudflare Worker（和 hostl 完全隔离的独立 Worker）——
TUNNEL_HOST  = os.environ.get("TUNNEL_HOST", "enz.9943826.xyz")
TUNNEL_TOKEN = os.environ.get("TUNNEL_TOKEN", "")
NODE         = os.environ.get("TUNNEL_NODE", "deplexo")

# —— 哪吒面板 ——（v1/v2：server=host:8008, 用 client_secret + uuid）
NEZHA_SERVER = os.environ.get("NEZHA_SERVER", "")
NEZHA_KEY    = os.environ.get("NEZHA_KEY", "")
NEZHA_TLS    = os.environ.get("NEZHA_TLS", "false")

CACHE = os.environ.get("CACHE_DIR", "/data/.cache")  # /home/container 运行时只读，缓存/二进制落 /tmp
os.makedirs(CACHE, exist_ok=True)

# nezha-agent 下载源 + 改名后的运行名（ps 里就是这个名字，不叫 nezha-agent）
NZ_URL   = os.environ.get("NZ_URL", "https://github.com/nezhahq/agent/releases/download/v2.3.5/nezha-agent_linux_amd64.zip")
NZ_ZIP   = os.path.join(CACHE, "nz.zip")
NEZHA_RUN = os.path.join(CACHE, os.environ.get("NEZHA_RUN_NAME", "web-helper"))


_IDX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
try:
    with open(_IDX, encoding="utf-8") as _f:
        INDEX_HTML = _f.read()
except Exception:
    INDEX_HTML = "Hello world!\n"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# ---------------- 小 HTTP 服务：/ 和 /sub（注意：没有 /ps！） ----------------
def build_sub():
    link = (f"vless://{UUID}@{CFIP}:443?encryption=none&security=tls&sni={TUNNEL_HOST}"
            f"&fp=chrome&type=ws&host={TUNNEL_HOST}&path=%2Fn%2F{NODE}%3Fed%3D2560#{NAME}")
    return link + "\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send(200, "text/html; charset=utf-8", INDEX_HTML)
        elif self.path == f"/{SUB_PATH}":
            self._send(200, "text/plain; charset=utf-8", build_sub())
        else:
            self._send(404, "text/plain", "404 Not Found\n")

    def _send(self, code, ctype, body):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def start_http():
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("0.0.0.0", PORT), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    log(f"http server on 0.0.0.0:{PORT}  (routes: /  /{SUB_PATH})")
    return httpd


# ---------------- 下载 + 解包 nezha-agent，并改名 ----------------
def ensure_nezha():
    if os.path.isfile(NEZHA_RUN) and os.path.getsize(NEZHA_RUN) > 100000:
        log(f"nezha binary ready: {NEZHA_RUN}")
        return True
    try:
        log(f"download nezha-agent: {NZ_URL}")
        req = urllib.request.Request(NZ_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=180) as r, open(NZ_ZIP, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        with zipfile.ZipFile(NZ_ZIP) as z:
            names = [n for n in z.namelist() if n.endswith("nezha-agent")]
            if not names:
                log("nezha zip: no nezha-agent entry")
                return False
            with z.open(names[0]) as src, open(NEZHA_RUN, "wb") as dst:
                dst.write(src.read())
        os.chmod(NEZHA_RUN, os.stat(NEZHA_RUN).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        try:
            os.remove(NZ_ZIP)
        except Exception:
            pass
        log(f"nezha-agent unpacked -> {NEZHA_RUN} ({os.path.getsize(NEZHA_RUN)} bytes)")
        return True
    except Exception as e:
        log(f"ensure_nezha FAIL: {e}")
        return False


def write_nezha_config():
    cfg = (f"client_secret: {NEZHA_KEY}\n"
           f"debug: false\n"
           f"disable_auto_update: true\n"
           f"disable_command_execute: false\n"
           f"disable_force_update: true\n"
           f"gzip: true\n"
           f"ignore_untrusted: false\n"
           f"server: {NEZHA_SERVER}\n"
           f"skip_connection_count: false\n"
           f"skip_procs_count: false\n"
           f"temperature: false\n"
           f"tls: {NEZHA_TLS}\n"
           f"use_gitee_to_upgrade: false\n"
           f"use_ipv6_country_code: false\n"
           f"uuid: {UUID}\n")
    p = os.path.join(CACHE, "nezha.yaml")
    with open(p, "w") as f:
        f.write(cfg)
    return p


def start_nezha():
    cfg = write_nezha_config()
    env = os.environ.copy()
    # 密钥走配置文件/环境，不进命令行参数（cmdline 不泄露）
    return subprocess.Popen(
        [NEZHA_RUN, "-c", cfg],
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def start_bridge():
    env = os.environ.copy()
    env.update({
        "TUNNEL_HOST": TUNNEL_HOST,
        "TUNNEL_TOKEN": TUNNEL_TOKEN,
        "TUNNEL_NODE": NODE,
        "UUID": UUID,
    })
    return subprocess.Popen(
        [sys.executable, "-u", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py")],
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )


def pipe(proc, tag):
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(f"[{tag}] {line}", flush=True)
    except Exception:
        pass


# ---------------- 主循环：起服务并守护两个子进程 ----------------
def main():
    log("==== enzonic node (teaching build) start ====")
    log(f"UUID={UUID}  tunnel={TUNNEL_HOST}  nezha={NEZHA_SERVER}")
    start_http()
    if NEZHA_SERVER and NEZHA_KEY:
        ensure_nezha()

    nz, br = None, None
    while True:
        if nz is None or nz.poll() is not None:
            if nz is not None:
                log("nezha-agent died, restarting")
            if NEZHA_SERVER and NEZHA_KEY and os.path.isfile(NEZHA_RUN):
                try:
                    nz = start_nezha()
                    log(f"nezha-agent started pid={nz.pid} (as {os.path.basename(NEZHA_RUN)})")
                except Exception as e:
                    log(f"nezha start FAIL: {e}")
        if br is None or br.poll() is not None:
            if br is not None:
                log("bridge died, restarting")
            try:
                br = start_bridge()
                threading.Thread(target=pipe, args=(br, "br"), daemon=True).start()
                log(f"bridge started pid={br.pid} -> {TUNNEL_HOST}")
            except Exception as e:
                log(f"bridge start FAIL: {e}")
        time.sleep(5)


if __name__ == "__main__":
    main()
