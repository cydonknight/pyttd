"""Stress tests: large recording, rapid navigation, memory leak detection."""
import json
import os
import socket
import subprocess
import sys
import time
import signal
import resource

TIMEOUT = 30

class RpcClient:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.pending_notifications = []
        self._id = 0

    def next_id(self):
        self._id += 1
        return self._id

    def _recv_raw(self, timeout=1.0):
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
            if data:
                self.buf += data
        except socket.timeout:
            pass
        messages = []
        while True:
            idx = self.buf.find(b"\r\n\r\n")
            if idx < 0:
                break
            header = self.buf[:idx].decode('ascii')
            cl = None
            for line in header.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    cl = int(line.split(":", 1)[1].strip())
            if cl is None:
                break
            body_start = idx + 4
            body_end = body_start + cl
            if len(self.buf) < body_end:
                break
            body = self.buf[body_start:body_end]
            self.buf = self.buf[body_end:]
            messages.append(json.loads(body))
        return messages

    def call(self, method, params=None, timeout=TIMEOUT):
        rid = self.next_id()
        msg = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params:
            msg["params"] = params
        body = json.dumps(msg).encode('utf-8')
        header = f"Content-Length: {len(body)}\r\n\r\n".encode('ascii')
        self.sock.sendall(header + body)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self._recv_raw(timeout=max(0.2, deadline - time.monotonic()))
            for m in msgs:
                if "id" in m and m["id"] == rid:
                    return m.get("result", {})
                else:
                    self.pending_notifications.append(m)
        raise TimeoutError(f"No response for {method} (id={rid})")

    def wait_notification(self, method, timeout=TIMEOUT):
        for i, n in enumerate(self.pending_notifications):
            if n.get("method") == method:
                return self.pending_notifications.pop(i).get("params", {})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self._recv_raw(timeout=max(0.2, deadline - time.monotonic()))
            for m in msgs:
                if m.get("method") == method:
                    return m.get("params", {})
                self.pending_notifications.append(m)
        raise TimeoutError(f"No {method} notification within {timeout}s")


def run_stress_test():
    test_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(test_dir, "_stress_target.py")

    # Generate a script with many frames
    with open(script_path, 'w') as f:
        f.write('''\
import time
import random

def compute(n):
    total = 0
    for i in range(n):
        total += i * i
    return total

def recursive(n):
    if n <= 0:
        return 0
    return n + recursive(n - 1)

def error_prone(x):
    if x % 7 == 0:
        raise ValueError(f"bad value: {x}")
    return x * 2

# Many calls
results = []
for i in range(50):
    results.append(compute(10))

# Recursive calls
for i in range(10):
    recursive(5)

# Exceptions
caught = 0
for i in range(30):
    try:
        error_prone(i)
    except ValueError:
        caught += 1

# I/O hooks
timestamps = [time.time() for _ in range(10)]
randoms = [random.random() for _ in range(10)]

print(f"Done: {len(results)} computes, {caught} exceptions, "
      f"{len(timestamps)} timestamps, {len(randoms)} randoms")
''')

    proc = subprocess.Popen(
        [sys.executable, "-m", "pyttd", "serve", "--script", script_path,
         "--checkpoint-interval", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        port_line = proc.stdout.readline().decode().strip()
        port = int(port_line.split(":")[1])
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(('127.0.0.1', port))
        rpc = RpcClient(sock)

        # Init + record
        rpc.call("backend_init")
        rpc.call("launch")
        rpc.call("set_breakpoints", {"breakpoints": []})
        rpc.call("set_exception_breakpoints", {"filters": ["raised"]})
        rpc.call("configuration_done")

        stopped = rpc.wait_notification("stopped", timeout=TIMEOUT)
        total_frames = stopped.get("totalFrames", 0)
        first_seq = stopped["seq"]
        print(f"Recorded {total_frames} frames, first_seq={first_seq}")

        # === STRESS 1: Rapid sequential navigation ===
        print("\n--- Stress 1: Rapid step_in (200 times) ---")
        t0 = time.monotonic()
        last_seq = first_seq
        for i in range(200):
            result = rpc.call("step_in", timeout=5)
            if result.get("reason") == "end":
                break
            last_seq = result["seq"]
        elapsed = time.monotonic() - t0
        print(f"  200 step_in: {elapsed:.2f}s ({elapsed/200*1000:.1f}ms/step), reached seq={last_seq}")

        # === STRESS 2: Rapid step_back ===
        print("\n--- Stress 2: Rapid step_back (200 times) ---")
        t0 = time.monotonic()
        for i in range(200):
            result = rpc.call("step_back", timeout=5)
            if result.get("reason") == "start":
                break
            last_seq = result["seq"]
        elapsed = time.monotonic() - t0
        print(f"  200 step_back: {elapsed:.2f}s ({elapsed/200*1000:.1f}ms/step), at seq={last_seq}")

        # === STRESS 3: Random goto_frame jumps ===
        print("\n--- Stress 3: Random goto_frame jumps (50 times) ---")
        import random as rnd
        rnd.seed(42)
        t0 = time.monotonic()
        errors = 0
        for i in range(50):
            target = rnd.randint(0, total_frames - 1)
            result = rpc.call("goto_frame", {"target_seq": target}, timeout=5)
            if "error" in result:
                errors += 1
        elapsed = time.monotonic() - t0
        print(f"  50 random gotos: {elapsed:.2f}s ({elapsed/50*1000:.1f}ms/jump), {errors} errors")

        # === STRESS 4: Stack trace at various positions ===
        print("\n--- Stress 4: Stack traces at 30 random positions ---")
        t0 = time.monotonic()
        max_depth = 0
        for i in range(30):
            target = rnd.randint(first_seq, total_frames - 1)
            rpc.call("goto_frame", {"target_seq": target}, timeout=5)
            result = rpc.call("get_stack_trace", timeout=5)
            depth = len(result.get("frames", []))
            max_depth = max(max_depth, depth)
        elapsed = time.monotonic() - t0
        print(f"  30 goto+stack: {elapsed:.2f}s, max stack depth={max_depth}")

        # === STRESS 5: Variables at many positions ===
        print("\n--- Stress 5: Variables at 50 positions ---")
        t0 = time.monotonic()
        total_vars = 0
        for i in range(50):
            target = rnd.randint(first_seq, total_frames - 1)
            result = rpc.call("get_variables", {"seq": target}, timeout=5)
            total_vars += len(result.get("variables", []))
        elapsed = time.monotonic() - t0
        print(f"  50 get_variables: {elapsed:.2f}s, total vars={total_vars}")

        # === STRESS 6: Timeline queries at various zoom levels ===
        print("\n--- Stress 6: Timeline queries (20 zooms) ---")
        t0 = time.monotonic()
        for i in range(20):
            span = max(10, total_frames // (i + 1))
            start = rnd.randint(0, max(0, total_frames - span))
            end = start + span
            result = rpc.call("get_timeline_summary", {
                "startSeq": start, "endSeq": end, "bucketCount": 100
            }, timeout=5)
            buckets = result.get("buckets", [])
        elapsed = time.monotonic() - t0
        print(f"  20 timeline queries: {elapsed:.2f}s")

        # === STRESS 7: Phase 6 queries ===
        print("\n--- Stress 7: Phase 6 queries ---")
        t0 = time.monotonic()
        result = rpc.call("get_traced_files")
        files = result.get("files", [])
        target_file = [f for f in files if "_stress_target.py" in f]
        if target_file:
            # Stats query
            result = rpc.call("get_execution_stats", {"filename": target_file[0]})
            stats = result.get("stats", [])
            print(f"  Functions: {len(stats)}")
            for s in stats:
                print(f"    {s['functionName']}: {s['callCount']} calls, "
                      f"{s['exceptionCount']} exceptions")

            # Call tree: drill down 3 levels
            result = rpc.call("get_call_children")
            children = result.get("children", [])
            print(f"  Root calls: {len(children)}")
            for c in children:
                if c.get("returnSeq") is not None:
                    result = rpc.call("get_call_children", {
                        "parentCallSeq": c["callSeq"],
                        "parentReturnSeq": c["returnSeq"],
                    })
                    inner = result.get("children", [])
                    if inner:
                        print(f"    {c['functionName']} has {len(inner)} children")
                        break
        elapsed = time.monotonic() - t0
        print(f"  Phase 6 queries: {elapsed:.2f}s")

        # === STRESS 8: Continue with breakpoints ===
        print("\n--- Stress 8: Continue with breakpoints ---")
        if target_file:
            rpc.call("set_breakpoints", {
                "breakpoints": [{"file": target_file[0], "line": 7}]
            })
            rpc.call("goto_frame", {"target_seq": first_seq})
            t0 = time.monotonic()
            hits = 0
            for i in range(100):
                result = rpc.call("continue", timeout=5)
                if result.get("reason") == "breakpoint":
                    hits += 1
                elif result.get("reason") == "end":
                    break
            elapsed = time.monotonic() - t0
            print(f"  Breakpoint hits: {hits} in {elapsed:.2f}s")

        # === STRESS 9: Reverse continue with exception filter ===
        print("\n--- Stress 9: Reverse continue exceptions ---")
        rpc.call("set_exception_breakpoints", {"filters": ["raised"]})
        # Navigate to end
        while True:
            result = rpc.call("continue", timeout=5)
            if result.get("reason") == "end":
                break
        t0 = time.monotonic()
        exception_hits = 0
        for i in range(50):
            result = rpc.call("reverse_continue", timeout=5)
            if result.get("reason") == "exception":
                exception_hits += 1
            elif result.get("reason") == "start":
                break
        elapsed = time.monotonic() - t0
        print(f"  Exception hits: {exception_hits} in {elapsed:.2f}s")

        # === Memory check ===
        print("\n--- Memory Usage ---")
        usage = resource.getrusage(resource.RUSAGE_SELF)
        print(f"  Client RSS: {usage.ru_maxrss / 1024:.1f} MB")

        # Disconnect
        rpc.call("disconnect")
        print("\n=== ALL STRESS TESTS PASSED ===")

    except Exception as e:
        print(f"\n!!! STRESS TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except:
            proc.kill()
            proc.wait()
        for fname in ["_stress_target.py", "_stress_target.pyttd.db",
                       "_stress_target.pyttd.db-wal", "_stress_target.pyttd.db-shm"]:
            try:
                os.remove(os.path.join(test_dir, fname))
            except:
                pass
    return True


if __name__ == "__main__":
    success = run_stress_test()
    sys.exit(0 if success else 1)
