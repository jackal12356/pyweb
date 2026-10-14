#!/usr/bin/env python3
# enzonic 反向隧道 agent：容器主动 WSS 连自己的 Cloudflare Worker（enz.9943826.xyz，多路复用），本地处理 VLESS
# 零第三方依赖。帧：type(1)|id(4)|payload  0 OPEN 1 DATA 2 CLOSE 3 PING
import os, ssl, socket, struct, asyncio, base64, time, json

HOST = os.environ.get("TUNNEL_HOST", "enz.9943826.xyz")
TOKEN = os.environ.get("TUNNEL_TOKEN", "")
NODE = os.environ.get("TUNNEL_NODE", "deplexo")
UUID_HEX = os.environ.get("UUID", "").replace("-", "")
CONNS = int(os.environ.get("TUNNEL_CONNS", "2"))
FALLBACK_IPS = ["104.16.0.1", "104.17.0.1", "172.67.0.1", "104.21.0.1"]


def log(m):
    print(f"[bridge {time.strftime('%H:%M:%S')}] {m}", flush=True)


def mkframe(op, payload=b""):
    n = len(payload)
    mask = os.urandom(4)
    if n < 126:
        hdr = bytes([0x82 if op == 2 else 0x80 | op, 0x80 | n])
    elif n < 65536:
        hdr = bytes([0x80 | op, 0x80 | 126]) + n.to_bytes(2, "big")
    else:
        hdr = bytes([0x80 | op, 0x80 | 127]) + n.to_bytes(8, "big")
    if n:
        # 快速异或掩码
        m = int.from_bytes(mask * ((n + 3) // 4), "big") >> (8 * ((4 - n % 4) % 4))
        payload = (int.from_bytes(payload, "big") ^ m).to_bytes(n, "big")
    return hdr + mask + payload


async def read_ws(reader):
    buf = b""
    while True:
        b1, b2 = await reader.readexactly(2)
        op = b1 & 0x0F
        ln = b2 & 0x7F
        if ln == 126:
            ln = int.from_bytes(await reader.readexactly(2), "big")
        elif ln == 127:
            ln = int.from_bytes(await reader.readexactly(8), "big")
        data = await reader.readexactly(ln) if ln else b""
        if op in (0x8, 0x9, 0xA):
            return op, data
        buf += data
        if b1 & 0x80:
            return op, buf


def parse_vless(d):
    if len(d) < 24 or d[0] != 0 or d[1:17].hex() != UUID_HEX:
        return "auth"
    off = 18 + d[17]
    if len(d) < off + 4:
        return None
    cmd = d[off]
    port = int.from_bytes(d[off + 1:off + 3], "big")
    at = d[off + 3]
    p = off + 4
    if at == 1:
        if len(d) < p + 4: return None
        addr, p = socket.inet_ntop(socket.AF_INET, d[p:p + 4]), p + 4
    elif at == 2:
        if len(d) < p + 1 or len(d) < p + 1 + d[p]: return None
        addr, p = d[p + 1:p + 1 + d[p]].decode(), p + 1 + d[p]
    elif at == 3:
        if len(d) < p + 16: return None
        addr, p = socket.inet_ntop(socket.AF_INET6, d[p:p + 16]), p + 16
    else:
        return "auth"
    return cmd, addr, port, d[p:]


DNS_CACHE = {}


async def doh(name):
    ctx = ssl.create_default_context()
    for srv in ("1.1.1.1", "1.0.0.1"):
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(srv, 443, ssl=ctx, server_hostname=srv), 8)
            w.write(f"GET /dns-query?name={name}&type=A HTTP/1.1\r\nHost: {srv}\r\nAccept: application/dns-json\r\nConnection: close\r\n\r\n".encode())
            await w.drain()
            body = (await asyncio.wait_for(r.read(), 8)).split(b"\r\n\r\n", 1)[1]
            w.close()
            ans = [a["data"] for a in json.loads(body).get("Answer", []) if a.get("type") == 1]
            if ans:
                return ans[0]
        except Exception as e:
            log(f"doh {name} via {srv} fail: {e}")
    return None


def bogus(ip):
    return ip.startswith(("0.", "127.")) or ip == "::" or ip == "::1"


async def resolve(addr):
    try:
        socket.inet_pton(socket.AF_INET, addr); return addr
    except Exception:
        pass
    if ":" in addr:
        return addr
    c = DNS_CACHE.get(addr)
    if c and c[1] > time.time():
        return c[0]
    ip = None
    try:
        res = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(addr, None, family=socket.AF_INET), 4)
        ips = [x[4][0] for x in res if not bogus(x[4][0])]
        ip = ips[0] if ips else None
    except Exception:
        ip = None
    if not ip:
        ip = await doh(addr)
    if ip:
        DNS_CACHE[addr] = (ip, time.time() + 300)
    return ip or addr


class Tunnel:
    def __init__(self, idx):
        self.idx = idx
        self.writer = None
        self.streams = {}  # id -> dict(buf, writer, task)
        self.lock = asyncio.Lock()

    async def send(self, t, sid, payload=b""):
        if not self.writer:
            return
        async with self.lock:
            self.writer.write(mkframe(2, bytes([t]) + struct.pack(">I", sid) + payload))
            await self.writer.drain()

    async def connect(self):
        ctx = ssl.create_default_context()
        try:
            ips = [HOST]
            socket.getaddrinfo(HOST, 443)
        except Exception:
            ips = FALLBACK_IPS
        last = None
        for ip in ips:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(ip, 443, ssl=ctx, server_hostname=HOST), 15)
                key = base64.b64encode(os.urandom(16)).decode()
                w.write((f"GET /agent?token={TOKEN}&node={NODE} HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
                         f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                         f"User-Agent: Mozilla/5.0\r\n\r\n").encode())
                await w.drain()
                status = await asyncio.wait_for(r.readline(), 15)
                while (await r.readline()) not in (b"\r\n", b""):
                    pass
                if b" 101 " not in status:
                    raise Exception(status.decode(errors="ignore").strip())
                return r, w
            except Exception as e:
                last = e
        raise Exception(f"connect fail: {last}")

    async def run(self):
        delay = 2
        while True:
            try:
                r, w = await self.connect()
                self.writer = w
                log(f"tunnel#{self.idx} connected to {HOST}")
                delay = 2
                pinger = asyncio.create_task(self.pinger())
                try:
                    await self.loop(r)
                finally:
                    pinger.cancel()
            except Exception as e:
                log(f"tunnel#{self.idx} error: {e}")
            self.writer = None
            for sid in list(self.streams):
                self.drop(sid)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def pinger(self):
        while True:
            await asyncio.sleep(25)
            await self.send(3, 0)

    async def loop(self, r):
        while True:
            op, data = await asyncio.wait_for(read_ws(r), 90)
            if op == 0x8:
                raise Exception("closed by server")
            if op == 0x9:
                async with self.lock:
                    self.writer.write(mkframe(0xA, data)); await self.writer.drain()
                continue
            if op == 0xA or len(data) < 5:
                continue
            t, sid, pl = data[0], struct.unpack(">I", data[1:5])[0], data[5:]
            if t == 0:
                self.streams[sid] = {"buf": b"", "w": None}
                if pl:
                    await self.on_data(sid, pl)
            elif t == 1:
                await self.on_data(sid, pl)
            elif t == 2:
                self.drop(sid)

    def drop(self, sid):
        s = self.streams.pop(sid, None)
        if s and s.get("w"):
            try: s["w"].close()
            except Exception: pass

    async def on_data(self, sid, pl):
        s = self.streams.get(sid)
        if s is None:
            return
        if s["w"] is not None:
            try:
                s["w"].write(pl)
                await asyncio.wait_for(s["w"].drain(), 30)
            except Exception:
                self.drop(sid); await self.send(2, sid)
            return
        if s.get("pending"):
            s["buf"] += pl
            return
        s["buf"] += pl
        v = parse_vless(s["buf"])
        if v is None:
            return
        if v == "auth" or v[0] != 1:
            log(f"stream {sid}: reject ({'auth' if v == 'auth' else 'udp/cmd ' + str(v[0])})")
            self.drop(sid); await self.send(2, sid); return
        _, addr, port, rest = v
        s["buf"] = rest
        s["pending"] = True
        asyncio.create_task(self.open_target(sid, addr, port, rest))

    async def open_target(self, sid, addr, port, rest):
        try:
            tr, tw = await asyncio.wait_for(asyncio.open_connection(await resolve(addr), port), 20)
        except Exception as e:
            log(f"stream {sid}: connect {addr}:{port} fail {e}")
            self.streams.pop(sid, None); await self.send(2, sid); return
        s = self.streams.get(sid)
        if s is None:
            tw.close(); return
        s["w"] = tw
        s["pending"] = False
        try:
            if s["buf"]:
                tw.write(s["buf"]); s["buf"] = b""
                await tw.drain()
        except Exception:
            pass
        await self.send(1, sid, b"\x00\x00")  # VLESS 响应头
        try:
            while True:
                d = await tr.read(32768)
                if not d:
                    break
                if sid not in self.streams:
                    break
                await self.send(1, sid, d)
        except Exception:
            pass
        if sid in self.streams:
            self.drop(sid)
            await self.send(2, sid)


async def watch_self():
    # 文件被更新就退出，由 app.py 自动拉起新版本（不影响哪吒）
    me = os.path.abspath(__file__)
    m0 = os.path.getmtime(me)
    while True:
        await asyncio.sleep(10)
        try:
            if os.path.getmtime(me) != m0:
                log("bridge.py updated, exiting for reload")
                os._exit(0)
        except Exception:
            pass


async def main():
    if not TOKEN:
        log("TUNNEL_TOKEN empty, exit"); return
    await asyncio.gather(watch_self(), *(Tunnel(i).run() for i in range(CONNS)))


if __name__ == "__main__":
    asyncio.run(main())
