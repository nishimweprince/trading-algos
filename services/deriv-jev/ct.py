"""Minimal cTrader Open API client (TLS + protobuf), read-only: app auth, account auth, symbols, trendbars. Never refreshes tokens."""
import os, ssl, socket, struct, time
from ta_plugin_ctrader._generated import OpenApiCommonMessages_pb2 as C, OpenApiMessages_pb2 as M, OpenApiModelMessages_pb2 as MM

def load_env(path):
    env = {}
    for ln in open(path):
        if '=' in ln and not ln.startswith('#'):
            k, v = ln.rstrip('\n').split('=', 1); env[k] = v
    return env

class Client:
    def __init__(self, host, port=5035):
        raw = socket.create_connection((host, port), timeout=30)
        self.s = ssl.create_default_context().wrap_socket(raw, server_hostname=host); self.s.settimeout(60)
        self.last = time.time(); self.mid = 0
    def _send(self, msg):
        self.mid += 1
        env = C.ProtoMessage(payloadType=msg.payloadType, payload=msg.SerializeToString(), clientMsgId=str(self.mid))
        b = env.SerializeToString(); self.s.sendall(struct.pack('>I', len(b)) + b); self.last = time.time()
    def _recv_exact(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk: raise ConnectionError('closed')
            buf += chunk
        return buf
    def _recv(self):
        n = struct.unpack('>I', self._recv_exact(4))[0]; env = C.ProtoMessage(); env.ParseFromString(self._recv_exact(n)); return env
    def heartbeat(self):
        if time.time() - self.last > 10: self._send(C.ProtoHeartbeatEvent())
    def call(self, msg, res_cls):
        self.heartbeat(); self._send(msg); want = res_cls().payloadType
        err = M.ProtoOAErrorRes().payloadType; err2 = C.ProtoErrorRes().payloadType
        while True:
            env = self._recv()
            if env.payloadType == want:
                r = res_cls(); r.ParseFromString(env.payload); return r
            if env.payloadType in (err, err2):
                e = (M.ProtoOAErrorRes if env.payloadType == err else C.ProtoErrorRes)(); e.ParseFromString(env.payload)
                raise RuntimeError(f'{e.errorCode}: {e.description}')
    def auth(self, env):
        self.call(M.ProtoOAApplicationAuthReq(clientId=env['CTRADER_CLIENT_ID'], clientSecret=env['CTRADER_CLIENT_SECRET']), M.ProtoOAApplicationAuthRes)
        self.acc = int(env['CTRADER_ACCOUNT_ID'])
        self.call(M.ProtoOAAccountAuthReq(ctidTraderAccountId=self.acc, accessToken=env['CTRADER_ACCESS_TOKEN']), M.ProtoOAAccountAuthRes)
    def symbols(self):
        return self.call(M.ProtoOASymbolsListReq(ctidTraderAccountId=self.acc), M.ProtoOASymbolsListRes).symbol
    def symbol_specs(self, ids):
        return self.call(M.ProtoOASymbolByIdReq(ctidTraderAccountId=self.acc, symbolId=list(ids)), M.ProtoOASymbolByIdRes).symbol
    def trader(self):
        return self.call(M.ProtoOATraderReq(ctidTraderAccountId=self.acc), M.ProtoOATraderRes).trader
    def m1(self, sid, t0_ms, t1_ms):
        r = self.call(M.ProtoOAGetTrendbarsReq(ctidTraderAccountId=self.acc, symbolId=sid, period=MM.ProtoOATrendbarPeriod.Value('M1'),
                                               fromTimestamp=t0_ms, toTimestamp=t1_ms), M.ProtoOAGetTrendbarsRes)
        out = []
        for b in r.trendbar:
            lo = b.low; out.append((b.utcTimestampInMinutes, (lo + b.deltaOpen) / 1e5, (lo + b.deltaHigh) / 1e5, lo / 1e5, (lo + b.deltaClose) / 1e5, b.volume))
        return out, r.hasMore
