"""ftx1_rigwrap - FTX-1 用 rigctld プロキシ

WSJT-X などの rigctld クライアントと rigctld（Hamlib）の間に入り、
  - 制御先を「操作バンド / その反対側 / MAIN 固定 / SUB 固定」に振り分ける
  - F/M の前に、制御先がメモリーチャンネル等なら VFO に戻す
  - DATA-U/DATA-L 設定後にナローが ON になっていたら OFF に戻す
を行う。設計は handoff/ftx1-rigwrap-design.md を参照。

使い方:
  pythonw ftx1_rigwrap.py [--rigctld 127.0.0.1:4534] [--listen 4535:follow --listen 4536:opposite]
                          [--bind 127.0.0.1] [--log PATH] [--debug]
  常駐起動は start_ftx1_rigwrap.vbs を使う（README.md 参照）
"""

import argparse
import logging
import logging.handlers
import os
import select
import socket
import socketserver
import sys
import threading
import time

VERSION = "1.9"

MODES = ("follow", "opposite", "main", "sub")
SIDE_DIGIT = {"Main": "0", "Sub": "1"}
OTHER = {"Main": "Sub", "Sub": "Main"}

# Hamlib tests/rigctl_parse.c の cmd_list より（ARG_NOVFO = VFO 引数を取らない）
SHORT_TO_LONG = {
    "F": "set_freq", "f": "get_freq", "M": "set_mode", "m": "get_mode",
    "I": "set_split_freq", "i": "get_split_freq", "X": "set_split_mode", "x": "get_split_mode",
    "K": "set_split_freq_mode", "k": "get_split_freq_mode", "S": "set_split_vfo", "s": "get_split_vfo",
    "N": "set_ts", "n": "get_ts", "L": "set_level", "l": "get_level", "U": "set_func", "u": "get_func",
    "P": "set_parm", "p": "get_parm", "G": "vfo_op", "g": "scan", "A": "set_trn", "a": "get_trn",
    "R": "set_rptr_shift", "r": "get_rptr_shift", "O": "set_rptr_offs", "o": "get_rptr_offs",
    "C": "set_ctcss_tone", "c": "get_ctcss_tone", "D": "set_dcs_code", "d": "get_dcs_code",
    "V": "set_vfo", "v": "get_vfo", "T": "set_ptt", "t": "get_ptt", "E": "set_mem", "e": "get_mem",
    "H": "set_channel", "h": "get_channel", "B": "set_bank", "_": "get_info",
    "J": "set_rit", "j": "get_rit", "Z": "set_xit", "z": "get_xit", "Y": "set_ant", "y": "get_ant",
    "w": "send_cmd", "W": "send_cmd_rx", "*": "reset", "b": "send_morse",
    "2": "power2mW", "4": "mW2power", "1": "dump_caps", "3": "dump_conf",
    "q": "quit", "Q": "quit",
}

NOVFO = {
    "set_parm", "get_parm", "set_trn", "get_trn", "set_vfo", "get_vfo", "set_channel", "get_channel",
    "get_info", "set_powerstat", "get_powerstat", "send_dtmf", "recv_dtmf", "send_cmd", "send_cmd_rx",
    "reset", "send_morse", "stop_morse", "wait_morse", "send_voice_mem", "stop_voice_mem", "get_dcd",
    "uplink", "set_twiddle", "get_twiddle", "set_cache", "get_cache", "power2mW", "mW2power",
    "dump_caps", "dump_conf", "dump_state", "chk_vfo", "set_vfo_opt", "get_vfo_info", "get_rig_info",
    "get_vfo_list", "get_modes", "get_clock", "set_clock", "halt", "pause", "password",
    "set_separator", "get_separator", "set_lock_mode", "get_lock_mode", "get_mode_bandwidths",
    "send_raw", "client_version", "hamlib_version", "set_gpio", "get_gpio", "set_conf", "get_conf",
    "test", "stream_caps", "stream_open", "stream_close", "stream_status", "stream_pause",
    "stream_resume", "stream_mute", "stream_unmute", "stream_metadata_read", "stream_drain",
    "stream_list", "quit",
}

# VFO 引数を取るコマンド（これ以外の未知のコマンドは手を加えずに中継する）
VFO_CMDS = (set(SHORT_TO_LONG.values())
            | {"set_ctcss_sql", "get_ctcss_sql", "set_dcs_sql", "get_dcs_sql"}) - NOVFO

NARROW_FIX_MODES = {"PKTUSB", "PKTLSB"}

# メモリー中はこの差未満の周波数設定を無視する（Hz）
MEM_KEEP_HZ = 1000

# メモリー中に来た M を保留しておく時間（秒）。この間に F でバンドが変われば VFO に設定する
PENDING_MODE_SEC = 10.0

# Hamlib のモード名 → FTX-1 の MD コード（newcat のモード表）
MODE_CODE = {
    "LSB": "1", "USB": "2", "CW": "3", "FM": "4", "AM": "5", "RTTY": "6", "CWR": "7",
    "PKTLSB": "8", "RTTYR": "9", "PKTFM": "A", "FMN": "B", "PKTUSB": "C", "AMN": "D", "PSK": "E",
}
EXT_PREFIXES = "+;|,"

# RPRT コード（-(hamlib rig_errcode_e)）
RIG_EIO = -6
RIG_ETIMEOUT = -5
RIG_EINVAL = -1
RIG_ENAVAIL = -11
RIG_ERJCTED = -9

log = logging.getLogger("ftx1_rigwrap")


# ---------------------------------------------------------------- 解析

class Cmd:
    """クライアントから来た1行を分解したもの"""

    def __init__(self, line):
        self.raw = line
        s = line.strip()
        self.prefix = ""
        if s and s[0] in EXT_PREFIXES:
            self.prefix, s = s[0], s[1:]
        self.token = ""     # 送られてきた形のまま（"F" や "\set_freq"）
        self.name = ""      # long name
        self.args = []
        if not s:
            return
        if s[0] == "\\":
            parts = s.split(None, 1)
            self.token = parts[0]
            self.name = parts[0][1:]
            rest = parts[1] if len(parts) > 1 else ""
        else:
            self.token = s[0]
            self.name = SHORT_TO_LONG.get(s[0], "")
            rest = s[1:]
        self.args = rest.split()

    @property
    def takes_vfo(self):
        return self.name in VFO_CMDS

    def build(self, vfo=None, args=None, prefix=None):
        a = list(self.args if args is None else args)
        if vfo:
            a.insert(0, vfo)
        p = self.prefix if prefix is None else prefix
        return p + " ".join([self.token] + a) + "\n"


def parse_rprt(line):
    try:
        return int(line.strip().split()[-1])
    except (ValueError, IndexError):
        return RIG_EIO


# ---------------------------------------------------------------- 制御用接続

class Control:
    """ラッパー自身が使う rigctld への接続（全クライアントで共有、vfo_opt=1、拡張応答）"""

    def __init__(self, host, port, timeout=6.0):
        self.addr = (host, port)
        self.timeout = timeout
        self.lock = threading.RLock()
        self.sock = None
        self.buf = b""
        self._band = None
        self._band_time = 0.0
        self.pending = {}       # side -> (モード名, 時刻)：メモリー中に保留した M

    # --- 低レベル
    def _connect(self):
        self.sock = socket.create_connection(self.addr, timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.buf = b""
        self._send_recv("+\\set_vfo_opt 1\n")

    def _close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _readline(self):
        while b"\n" not in self.buf:
            d = self.sock.recv(4096)
            if not d:
                raise ConnectionError("rigctld closed the connection")
            self.buf += d
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("ascii", "replace")

    def _send_recv(self, line, nlines=None):
        """応答を RPRT 行まで読む。nlines を指定したらその行数だけ読む（RPRT を返さないコマンド用）"""
        self.sock.sendall(line.encode("ascii"))
        lines = []
        while True:
            ln = self._readline()
            lines.append(ln)
            if nlines is not None:
                if len(lines) >= nlines:
                    return lines, 0
            elif "RPRT" in ln:
                return lines, parse_rprt(ln)

    def _leftover(self):
        """送信前に届いている読み残しの応答を返す（なければ b""）。
        あれば応答の対応がずれているので、呼び出し側で接続し直す"""
        data = self.buf
        while select.select([self.sock], [], [], 0)[0]:
            d = self.sock.recv(4096)
            if not d:
                break
            data += d
        return data

    def command(self, line, nlines=None):
        """1コマンドを実行し (応答行のリスト, RPRT) を返す。1回だけ再接続を試す"""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is not None:
                        left = self._leftover()
                        if left:
                            log.warning("制御用接続の応答がずれているため接続し直します (読み残し %r)",
                                        left[:200])
                            self._close()
                    if self.sock is None:
                        self._connect()
                    return self._send_recv(line, nlines)
                except socket.timeout:
                    log.error("rigctld timeout: %s", line.strip())
                    self._close()
                    return [], RIG_ETIMEOUT
                except OSError as e:
                    self._close()
                    if attempt == 2:
                        log.error("rigctld connection error: %s (%s)", e, line.strip())
                        return [], RIG_EIO

    # --- send_raw
    def raw(self, cat):
        lines, rc = self.command("+\\send_raw ; %s\n" % cat)
        if rc != 0:
            return None
        for ln in lines:
            if ln.startswith("Send raw answer:"):
                return ln.split(":", 1)[1].strip()
        return None

    # --- 操作バンド
    def op_band(self, fresh=False, ttl=0.5):
        with self.lock:
            if not fresh and self._band and time.monotonic() - self._band_time < ttl:
                return self._band
            ans = self.raw("VS;")
            if ans in ("VS0;", "VS1;"):
                self._band = "Main" if ans == "VS0;" else "Sub"
                self._band_time = time.monotonic()
                return self._band
            log.error("VS; unexpected answer: %r", ans)
            return None

    # --- メモリー → VFO
    def ensure_vfo(self, side, band, vm_ans=None):
        """side を VFO にする。band は現在の操作バンド。vm_ans は読み出し済みの VM 応答。
        戻り値 (ok, 変更前の VM 応答 or None, 補足)

        - FTX-1 は操作バンドでない側への VM を無視するため、その場合は
          操作バンドを一瞬だけ side に切り替えて VM を送り、元に戻す
        - VFO に戻すと VFO に残っていたモードになるため、メモリーのモードを引き継ぐ
        - 無線機とのやりとりを減らすため、VFO への切替・モード書き戻し・ナロー OFF・
          確認を1回の send_raw にまとめる"""
        d = SIDE_DIGIT[side]
        want = "VM%s00;" % d
        with self.lock:
            ans = vm_ans if vm_ans is not None else self.raw("VM%s;" % d)
            if ans == want:
                return True, None, ""
            pend = self.pending.pop(side, None)
            if pend and time.monotonic() - pend[1] < PENDING_MODE_SEC:
                code = MODE_CODE[pend[0]]
                restore = "MD%s%s;" % (d, code) + ("NA%s0;" % d if code in ("8", "C") else "")
                note = " 保留モード %s を設定" % pend[0]
            else:
                restore, note = self.mode_restore_cmds(side, self.raw("MD%s;" % d))
            if side == band:
                ans2 = self.raw("VM%s00;%sVM%s;" % (d, restore, d))
            else:
                tx = self.raw("TX;")
                if tx != "TX0;":
                    return False, ans, "送信中のため操作バンドを入れ替えない (%s)" % tx
                b = SIDE_DIGIT[band]
                ans2 = self.raw("VS%s;VM%s00;%sVS%s;VM%s;" % (d, d, restore, b, d))
                vs = self.raw("VS;")
                note = "操作バンドを一時入れ替え" + note
                if vs != "VS%s;" % b:
                    vs = self.raw("VS%s;VS;" % b)
                    log.error("操作バンドが元に戻らなかったため再設定: %s", vs)
                    note += "（復帰を再試行: %s）" % vs
                self._band, self._band_time = None, 0.0
            return ans2 == want, ans, note

    @staticmethod
    def mode_restore_cmds(side, md):
        """メモリーのときのモード md（"MD1C;" など）を書き戻す CAT 列と補足を返す"""
        d = SIDE_DIGIT[side]
        if not (md and len(md) == 5 and md.startswith("MD" + d)):
            return "", " モード引継ぎ不可 (%r)" % md
        if md[3] == "0":                            # W-FM は CAT で設定できない
            return "", " モード引継ぎなし (W-FM)"
        cmds = md
        if md[3] in ("8", "C"):                     # DATA 系にするとナローが ON になることがある
            cmds += "NA%s0;" % d
        return cmds, " モード引継ぎ %s" % md

    # --- 周波数の読み出し（Hamlib のキャッシュを使う。失敗したら FA/FB）
    def read_freq(self, side):
        lines, rc = self.command("+f %s\n" % side)
        if rc == 0:
            for ln in lines:
                if ln.startswith("Frequency:"):
                    try:
                        return int(float(ln.split(":", 1)[1]))
                    except ValueError:
                        break
        ans = self.raw("F%s;" % ("A" if side == "Main" else "B"))
        try:
            return int(ans[2:-1])
        except (TypeError, ValueError):
            return None

    # --- ナロー OFF
    def narrow_off(self, side):
        """ナローが ON なら OFF にする。戻り値 (ok, 変更したか)"""
        d = SIDE_DIGIT[side]
        ans = self.raw("NA%s;" % d)
        if ans == "NA%s0;" % d:
            return True, False
        ans2 = self.raw("NA%s0;NA%s;" % (d, d))
        return ans2 == "NA%s0;" % d, True

    def narrow_off_if_data(self, side):
        """DATA-U/DATA-L でナローが ON なら OFF にする（FM ナローなどはそのまま）。
        戻り値 (ok, 変更したか)。バンドが変わるとそのバンドのナロー状態が呼び出されるため"""
        d = SIDE_DIGIT[side]
        if self.raw("NA%s;" % d) != "NA%s1;" % d:
            return True, False
        md = self.raw("MD%s;" % d)
        if not (md and len(md) == 5 and md[3] in ("8", "C")):
            return True, False
        ans = self.raw("NA%s0;NA%s;" % (d, d))
        return ans == "NA%s0;" % d, True


# ---------------------------------------------------------------- クライアント1接続

class Handler(socketserver.BaseRequestHandler):
    # server 側で設定される: server.mode, server.control, server.upstream_addr

    def setup(self):
        self.mode = self.server.mode
        self.ctl = self.server.control
        self.routed = self.mode != "follow"
        self.client_vfo_mode = False   # follow でクライアント自身が set_vfo_opt 1 した
        self.wlock = threading.Lock()
        self.tag = "%s:%d" % (self.mode, self.server.server_address[1])
        self.up = None

    # --- 出力
    def to_client(self, data):
        with self.wlock:
            self.request.sendall(data.encode("ascii") if isinstance(data, str) else data)

    def reply_rc(self, cmd, rc, lines=None):
        """フックしたコマンドの応答。素の形式なら RPRT だけ、拡張なら制御用接続の応答をそのまま"""
        if cmd.prefix and lines:
            self.to_client("\n".join(lines) + "\n")
        else:
            self.to_client("RPRT %d\n" % rc)

    # --- 上流
    def open_upstream(self):
        up = socket.create_connection(self.server.upstream_addr, timeout=6.0)
        if self.routed:
            up.sendall(b"+\\set_vfo_opt 1\n")
            buf = b""
            while b"RPRT" not in buf or not buf.endswith(b"\n"):
                d = up.recv(4096)
                if not d:
                    raise ConnectionError("rigctld closed the connection")
                buf += d
        up.settimeout(None)
        return up

    def relay(self):
        try:
            while True:
                d = self.up.recv(4096)
                if not d:
                    break
                self.to_client(d)
        except OSError:
            pass
        finally:
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # --- メイン
    def handle(self):
        peer = "%s:%d" % self.client_address
        try:
            self.up = self.open_upstream()
        except OSError as e:
            log.warning("[%s] %s: rigctld に接続できないため切断 (%s)", self.tag, peer, e)
            return
        log.info("[%s] 接続 %s", self.tag, peer)
        threading.Thread(target=self.relay, daemon=True).start()
        f = self.request.makefile("rb")
        try:
            for raw in f:
                line = raw.decode("ascii", "replace")
                cmd = Cmd(line)
                if self.server.debug:
                    log.debug("[%s] < %s", self.tag, line.strip())
                out = self.dispatch(cmd)
                if out is not None:
                    self.up.sendall(out.encode("ascii"))
        except OSError:
            pass
        except Exception:
            # 想定外のエラーはこの接続だけ閉じる（ラッパー全体は止めない）
            log.exception("[%s] %s: 想定外のエラーのため切断", self.tag, peer)
        finally:
            try:
                self.up.close()
            except OSError:
                pass
            log.info("[%s] 切断 %s", self.tag, peer)

    # --- 振り分け。上流へ送る行を返す（None ならラッパーが応答済み）
    def target(self, fresh=False, band=None):
        if self.mode == "main":
            return "Main"
        if self.mode == "sub":
            return "Sub"
        if band is None:
            band = self.ctl.op_band(fresh=fresh)
        if band is None:
            return None
        return band if self.mode == "follow" else OTHER[band]

    def dispatch(self, cmd):
        n = cmd.name
        if not n:
            return cmd.raw if cmd.raw.endswith("\n") else cmd.raw + "\n"

        if n in ("set_freq", "set_mode"):
            self.hook_freq_mode(cmd)
            return None

        # chk_vfo はラッパーが答える。rigctld は「直前にコマンドを処理した別の接続」の
        # vfo_opt を返すため（Hamlib 4.7.3~rc で確認）、WSJT-X が VFO 付きの書式で
        # 送ってくることがある。
        # ただし rigctld にも chk_vfo を実行させる（答えは使わない）。rigctld は起動後に
        # 一度も chk_vfo を受けていないと dump_state の後半（最後の "done"）を出さず、
        # WSJT-X（Hamlib 4.7）が "done" を待ち続けて接続できないため
        if n == "chk_vfo":
            self.ctl.command("\\chk_vfo\n", nlines=1)
            v = 1 if (not self.routed and self.client_vfo_mode) else 0
            self.to_client(("ChkVFO: %d\n" if cmd.prefix else "%d\n") % v)
            return None

        if self.routed:
            if n in ("set_vfo", "set_vfo_opt"):
                log.info("[%s] %s を無視: %s", self.tag, n, cmd.raw.strip())
                self.to_client("RPRT 0\n")
                return None
            if n == "set_ptt":
                self.hook_ptt(cmd)
                return None
            if n == "set_split_vfo":
                if cmd.args[:1] == ["0"]:
                    self.to_client("RPRT 0\n")
                else:
                    log.warning("[%s] split ON を拒否: %s", self.tag, cmd.raw.strip())
                    self.to_client("RPRT %d\n" % RIG_ERJCTED)
                return None
            if n in ("set_split_freq", "set_split_mode", "set_split_freq_mode"):
                log.warning("[%s] %s は使用不可: %s", self.tag, n, cmd.raw.strip())
                self.to_client("RPRT %d\n" % RIG_ENAVAIL)
                return None
            if cmd.takes_vfo:
                t = self.target(fresh=False)
                if t is None:
                    self.to_client("RPRT %d\n" % RIG_EIO)
                    return None
                return cmd.build(vfo=t)
            return cmd.build()

        # follow: パススルー（クライアント自身の vfo_opt を記録）
        if n == "set_vfo_opt":
            self.client_vfo_mode = cmd.args[:1] == ["1"]
        return cmd.build()

    # --- F / M
    def hook_freq_mode(self, cmd):
        args = list(cmd.args)
        explicit = None
        if self.client_vfo_mode and args:          # follow でクライアントが VFO を付けてきた
            explicit = args.pop(0)
        band = self.ctl.op_band(fresh=True)
        if explicit in ("Main", "Sub"):
            side = explicit
        else:
            side = self.target(band=band)
        if side is None or band is None:
            self.to_client("RPRT %d\n" % RIG_EIO)
            return

        # M：制御先がメモリーなら無線機には送らず保留する（バンドが変わったら VFO に設定）
        if cmd.name == "set_mode" and args:
            d = SIDE_DIGIT[side]
            mode = args[0].upper()
            if self.ctl.raw("VM%s;" % d) != "VM%s00;" % d and mode in MODE_CODE:
                self.ctl.pending[side] = (mode, time.monotonic())
                log.info("[%s] %s: メモリー中のため %s を保留 (%s)", self.tag, side, mode,
                         cmd.raw.strip())
                self.to_client("RPRT 0\n")
                return
            self.ctl.pending.pop(side, None)

        # F：制御先がメモリーのときだけ処理する
        ok, before, note = True, None, ""
        if cmd.name == "set_freq" and args:
            d = SIDE_DIGIT[side]
            vm = self.ctl.raw("VM%s;" % d)
            if vm != "VM%s00;" % d:
                # 今の周波数との差が MEM_KEEP_HZ 未満なら何もしない
                # （WSJT-X の送り直しや接続時のわずかなずれでメモリーが解除されないように）
                try:
                    want = int(round(float(args[0])))
                except ValueError:
                    want = None
                cur = self.ctl.read_freq(side)
                if want is not None and cur is not None and abs(want - cur) < MEM_KEEP_HZ:
                    log.info("[%s] %s: メモリー中で周波数差 %dHz のため何もしない (%s)",
                             self.tag, side, want - cur, cmd.raw.strip())
                    self.to_client("RPRT 0\n")
                    return
                ok, before, note = self.ctl.ensure_vfo(side, band, vm_ans=vm)

        if not ok:
            log.error("[%s] %s を VFO にできないため %s を中止 (VM 応答 %r) %s",
                      self.tag, side, cmd.name, before, note)
            self.to_client("RPRT %d\n" % RIG_ERJCTED)
            return
        if before is not None:
            log.info("[%s] %s: %s → VFO (%s の前) %s", self.tag, side, before,
                     cmd.raw.strip(), note)

        lines, rc = self.ctl.command(cmd.build(vfo=side, args=args, prefix="+"))
        if rc == 0 and cmd.name == "set_mode" and args and args[0].upper() in NARROW_FIX_MODES:
            ok2, changed = self.ctl.narrow_off(side)
            if changed:
                log.info("[%s] %s: %s 後のナローを OFF (%s)", self.tag, side, args[0],
                         "OK" if ok2 else "失敗")
        if rc == 0 and cmd.name == "set_freq":
            ok2, changed = self.ctl.narrow_off_if_data(side)
            if changed:
                log.info("[%s] %s: 周波数変更後の DATA ナローを OFF (%s)", self.tag, side,
                         "OK" if ok2 else "失敗")
        if rc != 0:
            log.warning("[%s] %s -> RPRT %d", self.tag, cmd.raw.strip(), rc)
        self.reply_rc(cmd, rc, lines)

    # --- T（routed のみ）
    def hook_ptt(self, cmd):
        band = self.ctl.op_band(fresh=True)
        side = self.target(band=band)
        if side is None or band is None:
            self.to_client("RPRT %d\n" % RIG_EIO)
            return
        if side == band:
            lines, rc = self.ctl.command(cmd.build(vfo=side, prefix="+"))
            self.reply_rc(cmd, rc, lines)
            return
        # 操作バンドでない側の送信要求は実行せずに成功を返す（WSJT-X をエラーにしない）
        if cmd.args[:1] != ["0"]:
            log.info("[%s] 非操作バンド側 (%s) の送信要求を無視: %s", self.tag, side, cmd.raw.strip())
        self.to_client("RPRT 0\n")


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # 既定は stderr に出力（pythonw / exe では見えない）ため、ログに出す
        log.exception("接続 %s の処理で想定外のエラー", client_address)


def install_excepthooks():
    """捕捉されなかった例外をログに残す（コンソールなしで動かしても原因を追えるように）"""
    def main_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("想定外のエラーで終了", exc_info=(exc_type, exc, tb))

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        log.error("スレッド %s で想定外のエラー",
                  args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = main_hook
    threading.excepthook = thread_hook


# ---------------------------------------------------------------- 起動

def parse_hostport(s, default_host="127.0.0.1"):
    if ":" in s:
        h, p = s.rsplit(":", 1)
        return h or default_host, int(p)
    return default_host, int(s)


def parse_listen(s):
    try:
        p, m = s.split(":", 1)
        port = int(p)
    except ValueError:
        raise argparse.ArgumentTypeError("PORT:MODE の形式で指定してください: %s" % s)
    if m not in MODES:
        raise argparse.ArgumentTypeError("MODE は %s のいずれか: %s" % ("/".join(MODES), s))
    return port, m


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def fallback_log_path():
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "ftx1_rigwrap", "ftx1_rigwrap.log")


def open_log(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    return logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3,
                                                encoding="utf-8")


def setup_logging(path, debug):
    """ログを開く。書き込めなければ %LOCALAPPDATA%\\ftx1_rigwrap\\ に切り替える"""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    try:
        fh = open_log(path)
        failed = None
    except OSError as e:
        failed = (path, e)
        path = fallback_log_path()
        fh = open_log(path)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr is not None:          # --noconsole の exe では None
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    if failed:
        log.warning("ログ %s を開けないため %s に出力します (%s)", failed[0], path, failed[1])


def main(argv=None):
    ap = argparse.ArgumentParser(description="FTX-1 rigctld wrapper %s" % VERSION)
    ap.add_argument("--rigctld", default="127.0.0.1:4534", help="rigctld の HOST:PORT")
    ap.add_argument("--listen", action="append", type=parse_listen, metavar="PORT:MODE",
                    help="待ち受けポートとモード (follow/opposite/main/sub)。複数指定可")
    ap.add_argument("--bind", default="127.0.0.1", help="待ち受けアドレス")
    ap.add_argument("--log", default=os.path.join(base_dir(), "ftx1_rigwrap.log"))
    ap.add_argument("--debug", action="store_true", help="受信したコマンドもすべてログに出す")
    a = ap.parse_args(argv)
    listens = a.listen or [(4535, "follow"), (4536, "opposite")]

    setup_logging(a.log, a.debug)
    install_excepthooks()
    host, port = parse_hostport(a.rigctld)
    control = Control(host, port)
    log.info("ftx1_rigwrap %s 起動 rigctld=%s:%d", VERSION, host, port)

    servers = []
    for lport, mode in listens:
        try:
            srv = Server((a.bind, lport), Handler)
        except OSError as e:
            log.error("ポート %d を開けません (%s)。終了します", lport, e)
            for s in servers:
                s.server_close()
            return 1
        srv.mode, srv.control, srv.upstream_addr, srv.debug = mode, control, (host, port), a.debug
        servers.append(srv)
        log.info("待ち受け %s:%d [%s]", a.bind, lport, mode)

    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers[1:]:
            s.shutdown()
        for s in servers:
            s.server_close()
        log.info("終了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
