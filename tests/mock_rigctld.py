"""テスト用の簡易 rigctld + FTX-1 エミュレータ（実機の動きのうち、ラッパーに関係する部分だけ）"""
import socketserver
import threading
import time


CODE = {"0": "WFM", "1": "LSB", "2": "USB", "3": "CW", "4": "FM", "8": "PKTLSB", "C": "PKTUSB"}
NAME = {v: k for k, v in CODE.items()}


class Rig:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.vs = "0"
        self.vm = {"0": "00", "1": "00"}
        self.freq = {"0": 14074000, "1": 7074000}
        self.mode = {"0": "USB", "1": "LSB"}
        self.na = {"0": "0", "1": "0"}
        self.ag = {"0": "128", "1": "128"}          # AF GAIN
        self.ptt = "0"
        self.ptt_cache = "0"       # Hamlib の PTT キャッシュ（1秒）。Hamlib 経由の T / t だけが更新する
        self.ptt_cache_time = 0.0
        self.vfo_mode = {"0": "USB", "1": "USB"}   # VFO に戻ったときのモード
        self.cat = []          # 無線機に送られた CAT（記録）
        self.vs_history = []

    def side(self, vfo):
        if vfo in ("Main", "Sub"):
            return "0" if vfo == "Main" else "1"
        return self.vs

    def catcmd(self, c):
        """1つの CAT コマンドを処理し応答を返す（応答なしは ''）"""
        self.cat.append(c)
        if c == "VS;":
            return "VS%s;" % self.vs
        if c.startswith("VM") and len(c) == 4:
            return "VM%s%s;" % (c[2], self.vm[c[2]])
        if c.startswith("VS") and len(c) == 4:
            self.vs = c[2]
            self.vs_history.append(c[2])
            return ""
        if c.startswith("VM") and len(c) == 6:
            if c[2] == self.vs:          # 実機: 操作バンドでない側への VM は無視される
                if self.vm[c[2]] != "00" and c[3:5] == "00":
                    self.mode[c[2]] = self.vfo_mode[c[2]]
                self.vm[c[2]] = c[3:5]
            return ""
        if c.startswith("MD") and len(c) == 4:
            return "MD%s%s;" % (c[2], NAME.get(self.mode[c[2]], "2"))
        if c.startswith("MD") and len(c) == 5:
            self.mode[c[2]] = CODE[c[3]]
            if c[3] in "8C":
                self.na[c[2]] = "1"
            return ""
        if c == "TX;":
            return "TX%s;" % ("1" if self.ptt == "1" else "0")
        if c in ("TX0;", "TX1;"):
            self.ptt = c[2]            # send_raw 経由では Hamlib のキャッシュは更新されない
            return ""
        if c in ("FA;", "FB;"):
            return "%s%09d;" % (c[:2], self.freq["0" if c == "FA;" else "1"])
        if c.startswith("AG") and len(c) == 4:
            return "AG%s%s;" % (c[2], self.ag[c[2]])
        if c.startswith("AG") and len(c) == 7:
            self.ag[c[2]] = c[3:6]
            return ""
        if c.startswith("NA") and len(c) == 4:
            return "NA%s%s;" % (c[2], self.na[c[2]])
        if c.startswith("NA") and len(c) == 5:
            self.na[c[2]] = c[3]
            return ""
        if c[:2] in ("FA", "FB") and len(c) == 12:      # 周波数の書き込み（メモリー中は無視される）
            sd = "0" if c[1] == "A" else "1"
            if self.vm[sd] == "00":
                self.freq[sd] = int(c[2:11])
            return ""
        return "?;"


RIG = Rig()

# W-FM の側への m が rigctld に届いた回数（実機の Hamlib は -8 を返して rig を閉じる）
WFM_M_HITS = [0]

# rigctld の \chk_vfo は、直前にコマンドを処理した接続の vfo_opt を返す（Hamlib の実際の動作）
LAST_VFO_OPT = [False]
# rigctld は起動後に一度でも chk_vfo を受けると dump_state の最後に "done" を出す（Hamlib の実際の動作）
CHK_VFO_EXECUTED = [False]


class H(socketserver.StreamRequestHandler):
    def handle(self):
        vfo_opt = False
        for raw in self.rfile:
            line = raw.decode().strip()
            ext = line[:1] == "+"
            if ext:
                line = line[1:]
            parts = line.split()
            if not parts:
                continue
            cmd, args = parts[0], parts[1:]
            out = []
            with RIG.lock:
                rc = 0
                if cmd == "\\chk_vfo":
                    CHK_VFO_EXECUTED[0] = True
                    self.wfile.write(b"%d\n" % LAST_VFO_OPT[0])
                    continue
                if cmd == "\\set_vfo_opt":
                    vfo_opt = args[0] == "1"
                LAST_VFO_OPT[0] = vfo_opt
                if cmd == "\\set_vfo_opt":
                    pass                # vfo_opt は上で設定済み
                elif cmd == "\\dump_state":
                    out = ["1", "1051", "0"] + (["done"] if CHK_VFO_EXECUTED[0] else [])
                elif cmd == "\\send_raw":
                    s = " ".join(args[1:])
                    ans = ""
                    for c in [x + ";" for x in s.split(";") if x]:
                        ans = RIG.catcmd(c) or ans
                    out = ["Send raw answer: " + ans] if ext else [ans]
                elif cmd == "v":
                    out = ["Main" if RIG.vs == "0" else "Sub"]
                else:
                    vfo = args.pop(0) if vfo_opt else "currVFO"
                    sd = RIG.side(vfo)
                    if cmd == "f":
                        RIG.cat.append("F%s;" % "AB"[int(sd)])
                        out = [("Frequency: %d" if ext else "%d") % RIG.freq[sd]]
                    elif cmd == "F":
                        RIG.cat.append("F%s%09d;" % ("AB"[int(sd)], int(float(args[0]))))
                        if RIG.vm[sd] == "00":
                            RIG.freq[sd] = int(float(args[0]))
                        # メモリー中は拒否されるが RPRT 0（実機と同じ）
                    elif cmd == "m":
                        if RIG.mode[sd] == "WFM":
                            WFM_M_HITS[0] += 1
                            rc = -8
                        else:
                            out = [RIG.mode[sd], "3000"]
                    elif cmd == "M":
                        RIG.cat.append("MD%s:%s;" % (sd, args[0]))
                        RIG.mode[sd] = args[0]
                        if args[0] in ("PKTUSB", "PKTLSB"):
                            RIG.na[sd] = "1"
                    elif cmd == "t":
                        if time.time() - RIG.ptt_cache_time >= 1.0:      # キャッシュ切れ → 無線機を読む
                            RIG.ptt_cache, RIG.ptt_cache_time = RIG.ptt, time.time()
                        out = [RIG.ptt_cache]
                    elif cmd == "T":
                        RIG.cat.append("TX%s;" % args[0])
                        RIG.ptt = args[0]
                        RIG.ptt_cache, RIG.ptt_cache_time = args[0], time.time()
                    elif cmd == "s":
                        out = ["0", "Main"]
                    elif cmd == "V":
                        RIG.cat.append("VS-SET;")
                    else:
                        rc = -11
            resp = ""
            if ext:
                resp = "%s:\n" % cmd.lstrip("\\") + "".join(o + "\n" for o in out) + "RPRT %d\n" % rc
            elif out and rc == 0:
                resp = "".join(o + "\n" for o in out)
            else:
                resp = "RPRT %d\n" % rc
            self.wfile.write(resp.encode())


class S(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(port):
    srv = S(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
