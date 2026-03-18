"""Full end-to-end functional test of pyttd server + session navigation.
Acts as a JSON-RPC client to exercise the complete pipeline."""
import json
import os
import socket
import subprocess
import sys
import time
import signal

TIMEOUT = 15

class RpcClient:
    """JSON-RPC client that properly separates responses and notifications."""
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.pending_notifications = []
        self._id = 0

    def next_id(self):
        self._id += 1
        return self._id

    def _recv_raw(self, timeout=1.0):
        """Read from socket, parse complete messages."""
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
        """Send RPC and wait for response. Stashes notifications."""
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
        """Wait for a specific notification, checking stash first."""
        # Check stash
        for i, n in enumerate(self.pending_notifications):
            if n.get("method") == method:
                return self.pending_notifications.pop(i).get("params", {})
        # Wait for new
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self._recv_raw(timeout=max(0.2, deadline - time.monotonic()))
            for m in msgs:
                if m.get("method") == method:
                    return m.get("params", {})
                self.pending_notifications.append(m)
        raise TimeoutError(f"No {method} notification within {timeout}s")

    def drain_notifications(self, timeout=0.5):
        """Drain any pending notifications."""
        msgs = self._recv_raw(timeout=timeout)
        for m in msgs:
            if "id" not in m:
                self.pending_notifications.append(m)


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")

def assert_true(val, msg=""):
    if not val:
        raise AssertionError(f"{msg}: expected truthy, got {val!r}")

def main():
    test_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(test_dir, "_test_target.py")
    with open(script_path, 'w') as f:
        f.write('''\
def add(a, b):
    result = a + b
    return result

def greet(name):
    msg = f"Hello, {name}!"
    return msg

def exploder():
    raise ValueError("boom")

# Calls
x = add(1, 2)
y = add(3, 4)
z = greet("World")
print(f"x={x}, y={y}, z={z}")

# Exception
try:
    exploder()
except ValueError:
    pass

# Deep nesting
def outer():
    def middle():
        def inner():
            return 42
        return inner()
    return middle()

deep = outer()
print(f"deep={deep}")
''')

    proc = subprocess.Popen(
        [sys.executable, "-m", "pyttd", "serve", "--script", script_path,
         "--checkpoint-interval", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    passed = 0
    failed = 0
    def ok(msg):
        nonlocal passed
        passed += 1
        print(f"  [PASS] {msg}")
    def fail(msg):
        nonlocal failed
        failed += 1
        print(f"  [FAIL] {msg}")

    try:
        port_line = proc.stdout.readline().decode().strip()
        assert_true(port_line.startswith("PYTTD_PORT:"), f"Bad handshake: {port_line}")
        port = int(port_line.split(":")[1])

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(('127.0.0.1', port))
        rpc = RpcClient(sock)
        print(f"Server on port {port}")

        # === PHASE A: Init & Recording ===
        print("\n--- Phase A: Init & Recording ---")

        result = rpc.call("backend_init")
        assert_true("version" in result)
        assert_true("capabilities" in result)
        ok(f"backend_init: v={result['version']}, caps={result['capabilities']}")

        rpc.call("launch")
        ok("launch")

        rpc.call("set_breakpoints", {"breakpoints": [{"file": script_path, "line": 6}]})
        ok("set_breakpoints (line 6: greet body)")

        rpc.call("set_exception_breakpoints", {"filters": ["raised"]})
        ok("set_exception_breakpoints (raised)")

        rpc.call("configuration_done")
        ok("configuration_done -> recording started")

        stopped = rpc.wait_notification("stopped", timeout=TIMEOUT)
        assert_eq(stopped["reason"], "recording_complete")
        total_frames = stopped.get("totalFrames", 0)
        first_seq = stopped["seq"]
        ok(f"recording complete: {total_frames} frames, first_seq={first_seq}")

        # === PHASE B: Basic Queries ===
        print("\n--- Phase B: Basic Queries ---")

        result = rpc.call("get_threads")
        assert_eq(len(result["threads"]), 1)
        ok("get_threads")

        result = rpc.call("get_stack_trace")
        frames = result["frames"]
        assert_true(len(frames) >= 1)
        ok(f"get_stack_trace: {len(frames)} frames, top={frames[0]['name']}:{frames[0]['line']}")

        result = rpc.call("get_scopes", {"seq": first_seq})
        assert_eq(result["scopes"][0]["name"], "Locals")
        ok(f"get_scopes: varRef={result['scopes'][0]['variablesReference']}")

        result = rpc.call("get_variables", {"seq": first_seq})
        ok(f"get_variables at seq={first_seq}: {len(result['variables'])} vars")

        # === PHASE C: Forward Navigation ===
        print("\n--- Phase C: Forward Navigation ---")

        # step_into x5
        seqs = []
        for i in range(5):
            result = rpc.call("step_in")
            assert_true("seq" in result, f"step_in returned no seq: {result}")
            seqs.append(result["seq"])
        assert_true(all(seqs[i] < seqs[i+1] for i in range(len(seqs)-1)),
                    f"step_in seqs not monotonic: {seqs}")
        ok(f"step_into x5: seqs={seqs}")

        # step_over
        prev_seq = seqs[-1]
        result = rpc.call("next")
        assert_true(result["seq"] > prev_seq or result.get("reason") == "end")
        ok(f"step_over: seq={result['seq']}")

        # step_out
        result = rpc.call("step_out")
        assert_true("seq" in result)
        ok(f"step_out: seq={result['seq']}, reason={result.get('reason')}")

        # continue (should hit breakpoint on greet body line 6)
        result = rpc.call("continue")
        assert_true("seq" in result)
        ok(f"continue: reason={result.get('reason')}, seq={result['seq']}")

        # continue again (might hit breakpoint or exception)
        result = rpc.call("continue")
        ok(f"continue #2: reason={result.get('reason')}, seq={result.get('seq')}")

        # continue to end
        while result.get("reason") != "end":
            result = rpc.call("continue")
        ok(f"continue to end: seq={result['seq']}")

        # step_into at end — should return end
        result = rpc.call("step_in")
        assert_eq(result.get("reason"), "end", f"step_in at end should return 'end': {result}")
        ok("step_into at end stays at end")

        # === PHASE D: Reverse Navigation ===
        print("\n--- Phase D: Reverse Navigation ---")

        # step_back
        result = rpc.call("step_back")
        assert_true("seq" in result)
        end_seq = result["seq"]  # one before end
        ok(f"step_back from end: seq={result['seq']}")

        # step_back x3
        for i in range(3):
            result = rpc.call("step_back")
            assert_true(result["seq"] < end_seq or result.get("reason") == "start")
        ok(f"step_back x3: seq={result['seq']}")

        # reverse_continue (should hit exception or breakpoint)
        result = rpc.call("reverse_continue")
        assert_true(result.get("reason") in ("breakpoint", "exception", "start"),
                    f"Unexpected reason: {result}")
        ok(f"reverse_continue: reason={result['reason']}, seq={result['seq']}")

        # reverse_continue again
        result = rpc.call("reverse_continue")
        ok(f"reverse_continue #2: reason={result.get('reason')}, seq={result.get('seq')}")

        # === PHASE E: Goto / Targets / Restart ===
        print("\n--- Phase E: Goto / Targets / Restart ---")

        # goto_targets for add function body (line 2)
        result = rpc.call("goto_targets", {"filename": script_path, "line": 2})
        targets = result.get("targets", [])
        assert_true(len(targets) > 0, f"No goto targets at line 2: {result}")
        ok(f"goto_targets(line=2): {len(targets)} targets, first_seq={targets[0]['seq']}")

        # goto_frame to first target
        target_seq = targets[0]["seq"]
        result = rpc.call("goto_frame", {"target_seq": target_seq})
        assert_eq(result.get("seq"), target_seq)
        assert_eq(result.get("reason"), "goto")
        ok(f"goto_frame(seq={target_seq}): arrived at line={result.get('line')}")

        # Check stack at this position
        result = rpc.call("get_stack_trace")
        frames = result["frames"]
        top_frame = frames[0]
        ok(f"stack after goto: {' > '.join(f['name'] for f in frames)}")

        # Verify variables at this position
        result = rpc.call("get_variables", {"seq": target_seq})
        var_names = [v["name"] for v in result["variables"]]
        assert_true("a" in var_names, f"Expected 'a' in locals at add body: {var_names}")
        assert_true("b" in var_names, f"Expected 'b' in locals at add body: {var_names}")
        ok(f"variables at add(a,b): {var_names}")

        # goto_frame to non-line event (should snap)
        # Find a 'call' event
        result = rpc.call("goto_targets", {"filename": script_path, "line": 1})
        targets = result.get("targets", [])
        if targets:
            result = rpc.call("goto_frame", {"target_seq": targets[0]["seq"]})
            ok(f"goto_frame (snap): seq={result.get('seq')}")

        # goto_frame to invalid seq
        result = rpc.call("goto_frame", {"target_seq": 999999})
        assert_true("error" in result, f"Expected error for invalid seq: {result}")
        ok(f"goto_frame(invalid): error={result['error']}")

        # restart_frame
        result = rpc.call("get_stack_trace")
        if len(result["frames"]) >= 1:
            seq = result["frames"][0]["seq"]
            result = rpc.call("restart_frame", {"frame_seq": seq})
            ok(f"restart_frame: seq={result.get('seq')}, reason={result.get('reason')}")

        # === PHASE F: Evaluate ===
        print("\n--- Phase F: Evaluate ---")

        # Navigate to a spot where we have locals
        result = rpc.call("goto_targets", {"filename": script_path, "line": 3})
        if result.get("targets"):
            rpc.call("goto_frame", {"target_seq": result["targets"][0]["seq"]})

        # Hover eval
        result = rpc.call("evaluate", {"expression": "result", "context": "hover"})
        ok(f"evaluate(hover, 'result'): {result.get('result', 'N/A')}")

        # Watch eval
        result = rpc.call("evaluate", {"expression": "a", "context": "watch"})
        ok(f"evaluate(watch, 'a'): {result.get('result', 'N/A')}")

        # Repl eval
        result = rpc.call("evaluate", {"expression": "1+2", "context": "repl"})
        ok(f"evaluate(repl, '1+2'): {result.get('result', 'N/A')}")

        # Missing variable
        result = rpc.call("evaluate", {"expression": "nonexistent_xyz", "context": "hover"})
        ok(f"evaluate(missing var): {result.get('result', 'N/A')}")

        # === PHASE G: Timeline ===
        print("\n--- Phase G: Timeline ---")

        result = rpc.call("get_timeline_summary", {
            "startSeq": 0, "endSeq": total_frames, "bucketCount": 10
        })
        buckets = result.get("buckets", [])
        assert_true(len(buckets) > 0, "No timeline buckets")
        has_exception = any(b["hasException"] for b in buckets)
        ok(f"timeline: {len(buckets)} buckets, hasException={has_exception}")

        # Zoom into first bucket
        if buckets:
            b = buckets[0]
            result = rpc.call("get_timeline_summary", {
                "startSeq": b["startSeq"], "endSeq": b["endSeq"], "bucketCount": 5
            })
            sub_buckets = result.get("buckets", [])
            ok(f"timeline zoom: {len(sub_buckets)} sub-buckets")

        # === PHASE H: Phase 6 (CodeLens/CallTree queries) ===
        print("\n--- Phase H: Phase 6 Queries ---")

        # get_traced_files
        result = rpc.call("get_traced_files")
        files = result.get("files", [])
        assert_true(len(files) >= 1)
        target_files = [f for f in files if "_test_target.py" in f]
        assert_true(len(target_files) >= 1, f"Target file not in traced files: {files}")
        ok(f"get_traced_files: {len(files)} files")

        # get_execution_stats
        result = rpc.call("get_execution_stats", {"filename": target_files[0]})
        stats = result.get("stats", [])
        assert_true(len(stats) > 0, f"No execution stats for target file")
        func_names = {s["functionName"] for s in stats}
        assert_true("add" in func_names, f"'add' not in stats: {func_names}")
        assert_true("greet" in func_names, f"'greet' not in stats: {func_names}")
        assert_true("exploder" in func_names, f"'exploder' not in stats: {func_names}")
        add_stat = [s for s in stats if s["functionName"] == "add"][0]
        assert_eq(add_stat["callCount"], 2, "add should have 2 calls")
        assert_eq(add_stat["exceptionCount"], 0, "add should have 0 exceptions")
        exploder_stat = [s for s in stats if s["functionName"] == "exploder"][0]
        assert_eq(exploder_stat["callCount"], 1, "exploder should have 1 call")
        assert_eq(exploder_stat["exceptionCount"], 1, "exploder should have 1 exception")
        ok(f"get_execution_stats: {len(stats)} functions — add={add_stat['callCount']} calls, "
           f"exploder={exploder_stat['exceptionCount']} exceptions")

        # get_execution_stats for nonexistent file
        result = rpc.call("get_execution_stats", {"filename": "/nonexistent.py"})
        assert_eq(result.get("stats", []), [])
        ok("get_execution_stats(nonexistent): empty")

        # get_call_children (root)
        result = rpc.call("get_call_children")
        children = result.get("children", [])
        assert_true(len(children) >= 1)
        ok(f"get_call_children(root): {len(children)} root calls")
        for c in children[:3]:
            print(f"         {c['functionName']} d={c['depth']} "
                  f"seq={c['callSeq']}-{c['returnSeq']} exc={c['hasException']}")

        # Find module call and get its children
        module_calls = [c for c in children if c["functionName"] == "<module>"]
        if module_calls:
            mc = module_calls[0]
            result = rpc.call("get_call_children", {
                "parentCallSeq": mc["callSeq"],
                "parentReturnSeq": mc["returnSeq"],
            })
            mod_children = result.get("children", [])
            mod_func_names = [c["functionName"] for c in mod_children]
            assert_true("add" in mod_func_names,
                        f"'add' not in module children: {mod_func_names}")
            ok(f"get_call_children(<module>): {len(mod_children)} children: {mod_func_names}")

            # Drill into 'outer' to find middle -> inner
            outer_calls = [c for c in mod_children if c["functionName"] == "outer"]
            if outer_calls:
                oc = outer_calls[0]
                result = rpc.call("get_call_children", {
                    "parentCallSeq": oc["callSeq"],
                    "parentReturnSeq": oc["returnSeq"],
                })
                outer_children = result.get("children", [])
                mid_names = [c["functionName"] for c in outer_children]
                # co_qualname: nested funcs are "outer.<locals>.middle"
                assert_true(any("middle" in n for n in mid_names),
                            f"'middle' not in outer children: {mid_names}")
                ok(f"drill outer→middle: {mid_names}")

                # middle -> inner
                middle_calls = [c for c in outer_children
                                if "middle" in c["functionName"]]
                if middle_calls:
                    mc2 = middle_calls[0]
                    result = rpc.call("get_call_children", {
                        "parentCallSeq": mc2["callSeq"],
                        "parentReturnSeq": mc2["returnSeq"],
                    })
                    inner_children = result.get("children", [])
                    inner_names = [c["functionName"] for c in inner_children]
                    assert_true(any("inner" in n for n in inner_names),
                                f"'inner' not in middle children: {inner_names}")
                    ok(f"drill middle→inner: {inner_names}")

            # Check exploder has exception flag
            exploder_calls = [c for c in mod_children if c["functionName"] == "exploder"]
            if exploder_calls:
                assert_true(exploder_calls[0]["hasException"],
                            "exploder should have hasException=True")
                ok("exploder hasException=True")

        # get_call_children with invalid parent
        result = rpc.call("get_call_children", {"parentCallSeq": 999999})
        assert_eq(result.get("children", []), [])
        ok("get_call_children(invalid): empty")

        # === PHASE I: Deep stack verification ===
        print("\n--- Phase I: Deep Stack ---")

        result = rpc.call("goto_targets", {"filename": script_path, "line": 28})
        targets = result.get("targets", [])
        if targets:
            rpc.call("goto_frame", {"target_seq": targets[0]["seq"]})
            result = rpc.call("get_stack_trace")
            frames = result["frames"]
            frame_names = [f["name"] for f in frames]
            print(f"         Stack at inner: {' > '.join(frame_names)}")
            # co_qualname: "outer.<locals>.middle.<locals>.inner"
            assert_true(any("inner" in n for n in frame_names),
                        f"'inner' not in stack: {frame_names}")
            ok(f"deep stack: {len(frames)} frames")

        # === PHASE J: Edge cases ===
        print("\n--- Phase J: Edge Cases ---")

        # Step back from beginning
        rpc.call("goto_frame", {"target_seq": first_seq})
        result = rpc.call("step_back")
        # Should return start or stay at first
        ok(f"step_back from start: reason={result.get('reason')}, seq={result.get('seq')}")

        # goto_targets for line with no events
        result = rpc.call("goto_targets", {"filename": script_path, "line": 9999})
        assert_eq(result.get("targets", []), [])
        ok("goto_targets(line=9999): empty")

        # Evaluate at various positions
        rpc.call("goto_frame", {"target_seq": first_seq})
        result = rpc.call("evaluate", {"expression": "x.y.z", "context": "hover"})
        ok(f"evaluate dotted expr: {result.get('result', 'N/A')}")

        # Unknown RPC method
        try:
            result = rpc.call("totally_made_up_method")
            # Should get error response, not crash
            ok("unknown method: got response (no crash)")
        except Exception:
            ok("unknown method: handled gracefully")

        # === Disconnect ===
        print("\n--- Disconnect ---")
        rpc.call("disconnect")
        ok("disconnect")

        print(f"\n{'='*50}")
        print(f"RESULTS: {passed} passed, {failed} failed")
        if failed > 0:
            sys.exit(1)

    except Exception as e:
        print(f"\n!!! FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except:
            proc.kill()
            proc.wait()
        for fname in ["_test_target.py", "_test_target.pyttd.db",
                       "_test_target.pyttd.db-wal", "_test_target.pyttd.db-shm"]:
            try:
                os.remove(os.path.join(test_dir, fname))
            except:
                pass

if __name__ == "__main__":
    main()
