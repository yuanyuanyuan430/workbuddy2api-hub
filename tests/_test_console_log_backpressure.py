"""控制台日志读端不读时，网关必须照常应答（#642 回归）。

    python tests/_test_console_log_backpressure.py

`log()` 在 `send_response()` 之前被调用（log_request -> log_message -> log）。写
stderr 一旦**阻塞**，那一次请求的响应头就永远发不出去；而 /health /realm
/panel/status 因为在 log_message 静默白名单里照常 200，于是进程活着、端口在听、
健康检查绿，业务接口 100% 不可用。写*失败*（EPIPE）早就被 try/except 吃掉了，写
*阻塞* 才是抓不到的那一半：管道写端在缓冲满时就是原地等读端，不抛任何异常。

本套件分两段：

  1. 进程内——把 sys.stderr 换成一个 fileno() 指向**已写满**管道的包装对象，
     log() 必须立刻返回；看板 buffer 照常拿到每一条；读端接回来后队列补写，
     并留下"丢弃了 N 行"的说明。
  2. 真进程——用 pty 当子进程 stderr 且父进程从不读它（pty 缓冲约 1 KB，十几个
     请求就打满），再连打数百个会被记日志的请求。旧实现的请求线程会钉死在
     write() 上：除三个免日志端点外全部空响应。

第 2 段依赖 POSIX 的 pty（pty 缓冲小，几十个请求就能把事故形态做出来）；Windows
没有 pty，这一段打 [SKIP]，进程内那一段照常跑。
"""

import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the gateway lives one level up
PY = os.path.join(ROOT, "python", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import _lifecycle as life            # noqa: E402  (spare port + managed process)

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (label, (" -> " + detail) if detail else ""))


def answered(port, path, timeout=15):
    """(True, status line) when the peer answers at all; (False, why) otherwise."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except Exception as exc:
        return False, "connect failed: %s" % exc
    try:
        sock.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" % path)
                     .encode("ascii"))
        data = sock.recv(512)
    except Exception as exc:
        return False, "recv: %s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if not data:
        return False, "empty reply（连接被空关，正是要抓的形态）"
    head = data.split(b"\r\n", 1)[0].decode("latin-1")
    if not head.startswith("HTTP/"):
        return False, "不是状态行: %r" % head
    return True, head


# ---------------------------------------------------------------- 第 1 段：进程内
def blocked_stream(prefix="wb-console-"):
    """一个 fileno() 指向已写满管道的流：write() 会真的阻塞，正是要测的形态。"""
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    filled = 0
    while True:
        try:
            filled += os.write(write_fd, b"x" * 4096)
        except BlockingIOError:
            break
    os.set_blocking(write_fd, True)

    class Full(object):
        def fileno(self):
            return write_fd

        def write(self, text):
            os.write(write_fd, text.encode("utf-8"))   # 满管道上就是阻塞

        def flush(self):
            pass

    return Full(), read_fd, write_fd, filled


def drain_until(read_fd, needle, seconds):
    """把管道读空，直到读到 needle（或超时）。返回 (看到了没, 读到的文本)。"""
    os.set_blocking(read_fd, False)
    chunks = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            data = os.read(read_fd, 65536)
        except BlockingIOError:
            data = b""
        except OSError:
            break
        if data:
            chunks.append(data)
            if needle in data.decode("utf-8", "replace"):
                return True, b"".join(chunks).decode("utf-8", "replace")
        elif needle in b"".join(chunks).decode("utf-8", "replace"):
            return True, b"".join(chunks).decode("utf-8", "replace")
        else:
            time.sleep(0.02)
    text = b"".join(chunks).decode("utf-8", "replace")
    return needle in text, text


import wb_proxy                      # noqa: E402  (日志通道就是被测对象)

stream, read_fd, write_fd, filled = blocked_stream()
real_stderr = sys.stderr
# 换上去就不能中途换回来：写线程每次落盘都重新读 sys.stderr，换回来它就绕开这条
# 假管道直接写真实控制台，下面断言的"补写与丢弃说明"就看不到。
try:
    sys.stderr = stream
    started = time.time()
    for i in range(700):
        wb_proxy.log("backpressure line %d" % i)
    elapsed = time.time() - started

    print("  ..   stderr 管道的读端不读，缓冲已填 %d 字节" % filled)
    check("管道写满时 700 次 log() 仍然立刻返回（耗时 %.3fs）" % elapsed, elapsed < 5.0,
          "log() 在满管道上等了 %.1fs" % elapsed)
    logged = sum(1 for e in wb_proxy.LOG_BUFFER
                 if "backpressure line" in str(e.get("msg", "")))
    check("控制台写不进去时，看板 buffer 仍然一条不漏", logged == 700,
          "buffer 里只有 %d 条" % logged)
    check("超出的行被丢弃并计数（而不是无限堆在内存里）",
          wb_proxy._CONSOLE_QUEUE_MAX == 512 and len(wb_proxy._CONSOLE_QUEUE) <= 512
          and wb_proxy._CONSOLE_DROPPED > 0,
          "queued=%d dropped=%d" % (len(wb_proxy._CONSOLE_QUEUE),
                                    wb_proxy._CONSOLE_DROPPED))

    # 读端接回来：队列要能自己走完，并且明确写出丢了多少
    seen_drop, drained = drain_until(read_fd, "丢弃了", 20.0)
    check("读端恢复后补写完成，并说明丢弃了多少行", seen_drop,
          "读到 %d 字节，没找到丢弃说明" % len(drained))
    seen_tail, _ = drain_until(read_fd, "backpressure line 699", 20.0)
    check("积压的最后一行也补写出来了", seen_tail, "没读到 backpressure line 699")
finally:
    sys.stderr = real_stderr
    for fd in (read_fd, write_fd):
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------- 第 2 段：真进程
def pty_available():
    if os.name == "nt":
        return False
    try:
        import pty
    except ImportError:
        return False
    try:
        master, slave = pty.openpty()
    except Exception:
        return False
    os.close(master)
    os.close(slave)
    return True


if not pty_available():
    print("  [SKIP] 本平台没有 pty，跳过真进程那一段（进程内那一段已覆盖 log() 不阻塞）")
else:
    import pty

    work = tempfile.mkdtemp(prefix="wb-console-pressure-")
    store = os.path.join(work, "accounts")
    usage = os.path.join(work, "usage")
    os.makedirs(store, exist_ok=True)
    os.makedirs(usage, exist_ok=True)
    port = life.free_port()
    # pty 当 stderr：缓冲只有约 1 KB，而父进程从不读它——十几个请求就把写端堵死
    master_fd, slave_fd = pty.openpty()
    proc = life.spawn_managed(
        [PY, os.path.join(ROOT, "wb_proxy.py"), "--port", str(port), "--host", "127.0.0.1",
         "--accounts-dir", store],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=slave_fd,
        env=dict(os.environ, WB_PROXY_USAGE_DIR=usage))
    os.close(slave_fd)

    def wait_ready():
        for _ in range(60):
            time.sleep(0.5)
            ok, detail = answered(port, "/health", timeout=2)
            if ok and " 200" in detail:
                return True
            if proc.poll() is not None:
                return False
        return False

    try:
        check("备用端口 %d 上的网关已就绪" % port, wait_ready())
        if proc.poll() is not None:
            check("进程没有一早退出", False, "exit=%s" % proc.poll())
        else:
            sent = 0
            stuck = ""
            deadline = time.time() + 120
            while sent < 600 and time.time() < deadline:
                ok, detail = answered(port, "/v1/models", timeout=5)
                sent += 1
                if not ok:
                    stuck = "第 %d 个请求：%s" % (sent, detail)
                    break
            check("stderr 写端堵死时，600 个被记日志的请求全部有响应",
                  sent == 600 and not stuck, stuck or "只发了 %d 个" % sent)
            for path in ("/health", "/realm", "/panel/status", "/v1/models"):
                ok, detail = answered(port, path)
                check("堵住之后 %s 仍有响应" % path, ok, detail)
            check("进程还活着", proc.poll() is None, "exit=%s" % proc.poll())

            # 读端接回来：子进程应该把积压补写出来，并说明丢弃了多少
            os.set_blocking(master_fd, False)
            chunks = []
            seen = False
            end = time.time() + 30
            while time.time() < end:
                try:
                    data = os.read(master_fd, 65536)
                except BlockingIOError:
                    data = b""
                except OSError:
                    break
                if data:
                    chunks.append(data)
                elif "丢弃了".encode("utf-8") in b"".join(chunks):
                    seen = True
                    break
                else:
                    time.sleep(0.05)
            seen = seen or "丢弃了".encode("utf-8") in b"".join(chunks)
            check("控制台读端恢复后补写积压，并说明丢弃了多少行", seen,
                  "读到 %d 字节，没找到丢弃说明" % sum(len(c) for c in chunks))
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        life.stop_managed(proc)
        import shutil
        shutil.rmtree(work, ignore_errors=True)


print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
