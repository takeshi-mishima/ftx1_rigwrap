"""mock_rigctld を相手にラッパーの動作を確認する（python tests/test_rigwrap.py）"""
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import mock_rigctld  # noqa: E402
import ftx1_rigwrap  # noqa: E402

RIG = mock_rigctld.RIG
UP, P_FOLLOW, P_OPP, P_MAIN = 24534, 24535, 24536, 24537


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.buf = b""

    def q(self, line, nlines=1):
        self.s.sendall((line + "\n").encode())
        out = []
        while len(out) < nlines:
            while b"\n" not in self.buf:
                self.buf += self.s.recv(4096)
            ln, self.buf = self.buf.split(b"\n", 1)
            out.append(ln.decode())
        return out


fails = 0


def check(name, cond):
    global fails
    print(("OK   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1


_CTL = []


def ftx1_rigwrap_ctl():
    return _CTL[0].pending


def main():
    mock_rigctld.start(UP)
    log = os.path.join(tempfile.mkdtemp(), "w.log")
    args = ["--rigctld", "127.0.0.1:%d" % UP, "--listen", "%d:follow" % P_FOLLOW,
            "--listen", "%d:opposite" % P_OPP, "--listen", "%d:main" % P_MAIN, "--log", log]
    threading.Thread(target=ftx1_rigwrap.main, args=(args,), daemon=True).start()
    time.sleep(0.5)

    c1, c2, c3 = Client(P_FOLLOW), Client(P_OPP), Client(P_MAIN)
    import gc
    _CTL.append(next(o for o in gc.get_objects() if isinstance(o, ftx1_rigwrap.Control)))

    # --- 起動時ハンドシェイク
    c2.q("f")      # rigctld 側で直前の接続を vfo_opt=1 にする（Hamlib の chk_vfo の不具合の再現）
    check("follow chk_vfo=0（rigctld の答えに左右されない）", c1.q("\\chk_vfo") == ["0"])
    check("opposite chk_vfo=0", c2.q("\\chk_vfo") == ["0"])
    check("rigctld も chk_vfo を実行済み", mock_rigctld.CHK_VFO_EXECUTED[0])
    check("dump_state に done が出る（WSJT-X が接続できる）",
          c2.q("\\dump_state", 4) == ["1", "1051", "0", "done"])

    # --- 読み出しの振り分け（VS0: 操作=MAIN）
    check("follow f = MAIN", c1.q("f") == ["14074000"])
    check("opposite f = SUB", c2.q("f") == ["7074000"])
    check("opposite m = SUB", c2.q("m", 2) == ["LSB", "3000"])
    check("main f = MAIN", c3.q("f") == ["14074000"])
    check("opposite v はそのまま", c2.q("v") == ["Main"])

    # --- 操作バンドを入れ替えると opposite が追従
    RIG.vs = "1"
    time.sleep(0.6)  # キャッシュ切れ
    check("swap 後 follow f = SUB", c1.q("f") == ["7074000"])
    check("swap 後 opposite f = MAIN", c2.q("f") == ["14074000"])
    RIG.vs = "0"
    time.sleep(0.6)

    # --- F：SUB がメモリーなら VFO に戻してから設定
    RIG.vm["1"] = "11"
    RIG.cat.clear()
    check("opposite F → RPRT 0", c2.q("F 21074000") == ["RPRT 0"])
    check("SUB が VFO に戻った（操作バンドを一時入れ替え）", RIG.vm["1"] == "00")
    check("一時入れ替え後に操作バンドが MAIN に戻っている", RIG.vs == "0" and RIG.vs_history[-2:] == ["1", "0"])
    check("SUB 周波数が変わった", RIG.freq["1"] == 21074000)
    check("MAIN は変わらない", RIG.freq["0"] == 14074000)
    check("hamlib の set_vfo は使われない", "VS-SET;" not in RIG.cat and RIG.vs == "0")

    # --- 同じ周波数の F は何もしない（メモリーのまま）
    RIG.vm["1"] = "11"
    RIG.cat.clear()
    check("同じ周波数の F → RPRT 0", c2.q("F 21074000.000000") == ["RPRT 0"])
    check("メモリーのまま・F も VS も送られない", RIG.vm["1"] == "11"
          and not any(x.startswith(("FB0", "VS0;", "VS1;")) for x in RIG.cat))
    RIG.vm["1"] = "11"
    check("メモリー中・差 55Hz の F → 何もしない", c2.q("F 21074055") == ["RPRT 0"]
          and RIG.vm["1"] == "11" and RIG.freq["1"] == 21074000)
    RIG.vm["1"] = "00"
    check("VFO なら差 55Hz の F も設定する", c2.q("F 21074055") == ["RPRT 0"]
          and RIG.freq["1"] == 21074055)
    c2.q("F 21074000")

    # --- 送信中は入れ替えない
    RIG.vm["1"] = "11"
    RIG.ptt = "1"
    check("送信中に非操作側メモリー → RPRT -9", c2.q("F 24915000") == ["RPRT -9"])
    check("送信中は操作バンドを入れ替えない", RIG.vs == "0" and RIG.vm["1"] == "11")
    RIG.ptt = "0"
    RIG.vm["1"] = "00"

    # --- follow F：MAIN がメモリー
    RIG.vm["0"] = "11"
    check("follow F → RPRT 0", c1.q("F 28074000") == ["RPRT 0"])
    check("MAIN が VFO に戻り設定された", RIG.vm["0"] == "00" and RIG.freq["0"] == 28074000)

    # --- VFO に戻せない場合（エミュレータで VM 書き込みを無視させる）
    orig = RIG.catcmd

    def stubborn(c):
        if c.startswith("VM") and len(c) == 6:
            RIG.cat.append(c)
            return ""
        return orig(c)
    RIG.catcmd = stubborn
    RIG.vm["1"] = "11"
    check("VFO にできなければ RPRT -9", c2.q("F 18100000") == ["RPRT -9"])
    check("周波数は送られない", RIG.freq["1"] == 21074000)
    RIG.catcmd = orig
    RIG.vm["1"] = "00"

    # --- M はメモリーのまま、F で VFO に戻すときモードを引き継ぐ
    RIG.vm["1"] = "11"
    RIG.mode["1"] = "WFM"
    RIG.freq["1"] = 80000000
    RIG.vfo_mode["1"] = "USB"
    check("メモリー中の M PKTUSB → RPRT 0", c2.q("M PKTUSB -1") == ["RPRT 0"])
    check("M は保留（メモリー・W-FM のまま）", RIG.vm["1"] == "11" and RIG.mode["1"] == "WFM")
    check("同じ周波数の F → メモリーのまま", c2.q("F 80000000") == ["RPRT 0"] and RIG.vm["1"] == "11")
    check("F で VFO に戻る", c2.q("F 7041000") == ["RPRT 0"] and RIG.vm["1"] == "00"
          and RIG.freq["1"] == 7041000)
    check("VFO に戻ったら保留モード PKTUSB", RIG.mode["1"] == "PKTUSB")
    check("保留モード設定後のナロー OFF", RIG.na["1"] == "0")
    # 保留の期限切れ → メモリーのモードを引き継ぐ
    RIG.vm["1"] = "11"
    RIG.mode["1"] = "CW"
    c2.q("M PKTUSB -1")
    side_pend = ftx1_rigwrap_ctl()["Sub"]
    ftx1_rigwrap_ctl()["Sub"] = (side_pend[0], side_pend[1] - 60)
    check("期限切れなら F でメモリーのモード CW", c2.q("F 10136000") == ["RPRT 0"]
          and RIG.mode["1"] == "CW")
    RIG.vm["1"] = "11"
    RIG.mode["1"] = "WFM"
    check("W-FM のメモリーから F", c2.q("F 7074000") == ["RPRT 0"] and RIG.mode["1"] == "USB")

    # --- M PKTUSB → ナロー OFF
    check("opposite M PKTUSB → RPRT 0", c2.q("M PKTUSB -1") == ["RPRT 0"])
    check("SUB が PKTUSB", RIG.mode["1"] == "PKTUSB")
    check("SUB ナロー OFF", RIG.na["1"] == "0")
    check("MAIN のモードは不変", RIG.mode["0"] == "USB")
    check("follow M PKTUSB", c1.q("M PKTUSB -1") == ["RPRT 0"] and RIG.na["0"] == "0")
    RIG.cat.clear()
    c1.q("M CW 0")
    check("CW ではナロー補正しない", not any(x.startswith("NA") for x in RIG.cat))

    # --- F の後のナロー（バンドのナロー状態が呼び出される場合）
    RIG.mode["0"] = "PKTUSB"
    RIG.na["0"] = "1"
    check("DATA-U でナロー ON → F 後に OFF", c1.q("F 144460000") == ["RPRT 0"] and RIG.na["0"] == "0")
    RIG.mode["0"] = "FM"
    RIG.na["0"] = "1"
    check("FM のナローはそのまま", c1.q("F 433000000") == ["RPRT 0"] and RIG.na["0"] == "1")
    RIG.na["0"] = "0"
    RIG.mode["0"] = "USB"

    # --- PTT
    RIG.cat.clear()
    check("opposite T 1 → RPRT 0（無視）", c2.q("T 1") == ["RPRT 0"])
    check("opposite T 0 → RPRT 0（送らない）", c2.q("T 0") == ["RPRT 0"])
    check("TX は送られていない", not any(x.startswith("TX") for x in RIG.cat))
    check("main T 1（操作バンド）→ 実行", c3.q("T 1") == ["RPRT 0"] and RIG.ptt == "1")
    check("follow T 0 パススルー", c1.q("T 0") == ["RPRT 0"] and RIG.ptt == "0")

    # --- 拒否・無視
    RIG.cat.clear()
    check("opposite V は無視", c2.q("V Sub") == ["RPRT 0"] and "VS-SET;" not in RIG.cat)
    check("opposite S 0 は無視", c2.q("S 0 Main") == ["RPRT 0"])
    check("opposite S 1 は拒否", c2.q("S 1 Main") == ["RPRT -9"])
    check("opposite I は使用不可", c2.q("I 14075000") == ["RPRT -11"])
    check("opposite set_vfo_opt は無視", c2.q("\\set_vfo_opt 1") == ["RPRT 0"])
    check("その後も chk_vfo=0", c2.q("\\chk_vfo") == ["0"])

    # --- 拡張応答
    r = c2.q("+F 14074000", 2)
    check("opposite +F は拡張応答", r[-1] == "RPRT 0" and r[0].endswith(":"))

    # --- follow でクライアント自身が vfo_opt（audio_swap 型）
    c4 = Client(P_FOLLOW)
    c4.q("\\set_vfo_opt 1")
    check("follow vfo_opt 後 f Sub", c4.q("f Sub") == ["14074000"])
    RIG.vm["1"] = "11"
    check("follow vfo_opt 後 F Sub", c4.q("F Sub 50313000") == ["RPRT 0"])
    check("SUB が VFO に戻り設定", RIG.vm["1"] == "00" and RIG.freq["1"] == 50313000)
    check("follow vfo_opt 1 のクライアントには chk_vfo=1", c4.q("\\chk_vfo") == ["1"])

    # --- 制御用接続の応答のずれ（読み残し）→ 接続し直して正しい応答を得る
    ctl = _CTL[0]
    with ctl.lock:
        ctl.sock.sendall(b"+\\send_raw ; FB;\n")     # 応答を読まずに残す
    time.sleep(0.3)
    RIG.vm["0"], RIG.vs = "00", "0"
    check("ずれ発生後も follow F → RPRT 0", c1.q("F 14075000") == ["RPRT 0"]
          and RIG.freq["0"] == 14075000)
    check("ずれを検出して接続し直した", "応答がずれている" in open(log, encoding="utf-8").read())

    print(open(log, encoding="utf-8").read())
    print("FAILS:", fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
