import json
import logging
import os
from pyttd.models.db import db
from pyttd.replay import ReplayController

logger = logging.getLogger(__name__)

MAX_CONDITIONAL_SEARCH = 10_000

SAFE_BUILTINS = {
    # Types and constructors
    'len': len, 'str': str, 'int': int, 'float': float, 'bool': bool,
    'list': list, 'dict': dict, 'tuple': tuple, 'set': set, 'type': type,
    'bytes': bytes, 'bytearray': bytearray, 'frozenset': frozenset,
    'complex': complex, 'range': range, 'slice': slice,
    # Type checking
    'isinstance': isinstance, 'issubclass': issubclass,
    # Math
    'abs': abs, 'min': min, 'max': max, 'sum': sum,
    'round': round, 'pow': pow, 'divmod': divmod,
    # Comparison and logic
    'all': all, 'any': any,
    # Iteration
    'enumerate': enumerate, 'zip': zip, 'map': map, 'filter': filter,
    'reversed': reversed, 'sorted': sorted, 'iter': iter, 'next': next,
    # Attribute access (read-only)
    'hasattr': hasattr, 'getattr': getattr,
    # String/repr
    'repr': repr, 'ascii': ascii, 'chr': chr, 'ord': ord,
    'format': format, 'bin': bin, 'hex': hex, 'oct': oct,
    # Object identity
    'id': id, 'hash': hash, 'callable': callable,
    # Constants
    'True': True, 'False': False, 'None': None,
}


class Session:
    def __init__(self):
        self.run_id = None
        self.current_frame_seq = None
        self.state = "idle"  # "idle" | "recording" | "replay"
        self.breakpoints = []      # [{file: str, line: int, condition?, hitCondition?, logMessage?}, ...]
        self.exception_filters = []  # ["raised", "uncaught"]
        self.function_breakpoints = []  # [{name: str, condition?, hitCondition?}]
        self.data_breakpoints = []  # [{variableName: str}]
        self._bp_hit_counts = {}  # (file, line) -> hit_count
        self._fn_bp_hit_counts = {}  # name -> hit_count
        self._log_messages = []  # accumulated log messages from logpoints
        self._condition_errors = []  # [{seq, condition, error}, ...] from last navigation
        self.current_stack = []    # [{seq, name, file, line, depth}, ...]
        self.first_line_seq = None
        self.last_line_seq = None
        self.replay_controller = ReplayController()
        self._stack_cache = {}  # (seq, thread_id) -> stack_snapshot (DAP order)
        self._var_ref_cache = {}  # ref_id -> (seq, name) for expandable variables
        self.current_thread_id = None
        self.known_threads = {}  # {thread_id: "Thread Name"}

    # --- Private query helpers ---

    def _fetch_frame(self, seq):
        """Single frame by sequence_no."""
        return db.fetchone(
            "SELECT * FROM executionframes WHERE run_id = ? AND sequence_no = ?",
            (str(self.run_id), seq))

    def _fetch_line_after(self, after_seq, extra_conditions="", extra_params=()):
        """First line event after after_seq."""
        return db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND sequence_no > ?"
            + extra_conditions +
            " ORDER BY sequence_no LIMIT 1",
            (str(self.run_id), after_seq) + extra_params)

    def _fetch_line_before(self, before_seq, extra_conditions="", extra_params=()):
        """First line event before before_seq."""
        return db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND sequence_no < ?"
            + extra_conditions +
            " ORDER BY sequence_no DESC LIMIT 1",
            (str(self.run_id), before_seq) + extra_params)

    def enter_replay(self, run_id, first_line_seq: int):
        self.run_id = run_id
        self.state = "replay"
        self.current_frame_seq = first_line_seq

        # Cache boundary seqs
        self.first_line_seq = first_line_seq
        last = db.fetchone(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY sequence_no DESC LIMIT 1",
            (str(self.run_id),))
        self.last_line_seq = last.sequence_no if last else first_line_seq

        # Identify main thread from the first recorded event (sequence_no 0)
        first_event = self._fetch_frame(0)
        main_thread_id = first_event.thread_id if first_event else None

        # Discover threads, labeling the main thread correctly
        thread_rows = db.fetchall(
            "SELECT DISTINCT thread_id FROM executionframes WHERE run_id = ?",
            (str(self.run_id),))
        self.known_threads = {}
        for row in thread_rows:
            if row.thread_id == main_thread_id:
                self.known_threads[row.thread_id] = "Main Thread"
            else:
                self.known_threads[row.thread_id] = f"Thread {row.thread_id}"

        # Set current thread from first line event
        first = self._fetch_frame(first_line_seq)
        self.current_thread_id = first.thread_id if first else 0

        # Build initial stack at first_line_seq
        self.current_stack = self._build_stack_at(first_line_seq)

    def set_breakpoints(self, breakpoints: list[dict]):
        self.breakpoints = [
            {**bp, 'file': os.path.realpath(bp['file'])} if 'file' in bp else bp
            for bp in breakpoints
        ]

    def set_exception_filters(self, filters: list[str]):
        self.exception_filters = filters

    def set_function_breakpoints(self, breakpoints: list[dict]):
        self.function_breakpoints = breakpoints  # [{name, condition?, hitCondition?}]
        self._fn_bp_hit_counts = {}

    def set_data_breakpoints(self, breakpoints: list[dict]):
        self.data_breakpoints = breakpoints  # [{variableName}]

    def verify_function_breakpoints(self, breakpoints: list[dict]) -> list[dict]:
        results = []
        for bp in breakpoints:
            name = bp.get('name', '')
            if self.state == "replay" and name:
                exists = db.fetchone(
                    "SELECT 1 FROM executionframes"
                    " WHERE run_id = ? AND frame_event = 'call'"
                    " AND function_name LIKE '%' || ? || '%'"
                    " LIMIT 1",
                    (str(self.run_id), name)) is not None
                if not exists:
                    results.append({'verified': False, 'message': f"Function '{name}' not found in recording"})
                else:
                    results.append({'verified': True})
            else:
                results.append({'verified': True})
        return results

    def get_threads(self) -> list[dict]:
        return [{"id": tid, "name": name} for tid, name in self.known_threads.items()]

    def _require_replay(self):
        if self.state != "replay":
            raise RuntimeError("Session not in replay state")

    # --- Forward Navigation ---

    def step_into(self) -> dict:
        self._require_replay()
        frame = self._fetch_line_after(self.current_frame_seq)
        if frame is None:
            return self._navigate_to(self.last_line_seq, "end")
        return self._navigate_to(frame.sequence_no, "step")

    def step_over(self) -> dict:
        self._require_replay()
        current = self._get_current_frame()
        if current is None:
            return self._navigate_to(self.last_line_seq, "end")
        current_depth = current.call_depth
        current_thread = current.thread_id
        frame = self._fetch_line_after(
            self.current_frame_seq,
            " AND call_depth <= ? AND thread_id = ?",
            (current_depth, current_thread))
        if frame is None:
            return self._navigate_to(self.last_line_seq, "end")
        return self._navigate_to(frame.sequence_no, "step")

    def step_out(self) -> dict:
        self._require_replay()
        current = self._get_current_frame()
        if current is None:
            return self._navigate_to(self.last_line_seq, "end")
        current_depth = current.call_depth
        current_thread = current.thread_id
        current_func = current.function_name

        if current_depth == 0:
            return self._navigate_to(self.last_line_seq, "end")

        # Find exit event, skipping coroutine suspension returns.
        # A suspension return for a coroutine/generator is followed by a
        # matching call (resume) at the same depth for the same function.
        search_after = self.current_frame_seq
        exit_event = None
        for _ in range(100):  # bounded search to avoid infinite loops
            candidate = db.fetchone(
                "SELECT * FROM executionframes"
                " WHERE run_id = ? AND frame_event IN ('return', 'exception_unwind')"
                " AND call_depth = ? AND thread_id = ?"
                " AND sequence_no > ?"
                " ORDER BY sequence_no LIMIT 1",
                (str(self.run_id), current_depth, current_thread, search_after))
            if candidate is None:
                break

            # Check if this is a coroutine suspension (return followed by resume)
            if (candidate.frame_event == 'return' and
                    getattr(candidate, 'is_coroutine', False)):
                resume = db.fetchone(
                    "SELECT * FROM executionframes"
                    " WHERE run_id = ? AND frame_event = 'call'"
                    " AND call_depth = ? AND function_name = ?"
                    " AND thread_id = ? AND sequence_no > ?"
                    " ORDER BY sequence_no LIMIT 1",
                    (str(self.run_id), current_depth, candidate.function_name,
                     current_thread, candidate.sequence_no))
                if resume is not None:
                    # Suspension — skip past the resume and keep searching
                    search_after = resume.sequence_no
                    continue

            exit_event = candidate
            break

        if exit_event is None:
            return self._navigate_to(self.last_line_seq, "end")

        parent_line = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND call_depth = ? AND thread_id = ?"
            " AND sequence_no > ?"
            " ORDER BY sequence_no LIMIT 1",
            (str(self.run_id), current_depth - 1, current_thread,
             exit_event.sequence_no))

        if parent_line is None and exit_event.frame_event == 'exception_unwind':
            parent_line = db.fetchone(
                "SELECT * FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'line'"
                " AND call_depth < ? AND thread_id = ?"
                " AND sequence_no > ?"
                " ORDER BY sequence_no LIMIT 1",
                (str(self.run_id), current_depth, current_thread,
                 exit_event.sequence_no))

        if parent_line is None:
            return self._navigate_to(self.last_line_seq, "end")
        return self._navigate_to(parent_line.sequence_no, "step")

    def continue_forward(self) -> dict:
        self._require_replay()
        candidates = []
        self._log_messages = []
        self._condition_errors = []

        for bp in self.breakpoints:
            if 'file' not in bp or 'line' not in bp:
                continue
            condition = bp.get('condition', '')
            hit_condition = bp.get('hitCondition', '')
            log_message = bp.get('logMessage', '')
            hit = self._find_conditional_hit_forward(bp['file'], bp['line'],
                                                      condition, self.current_frame_seq,
                                                      hit_condition=hit_condition,
                                                      log_message=log_message)
            if hit is not None:
                if log_message:
                    # Log points don't stop; they just emit messages
                    pass
                else:
                    candidates.append((hit, "breakpoint"))

        # Function breakpoints
        for fbp in self.function_breakpoints:
            name = fbp.get('name', '')
            if not name:
                continue
            snap = self._find_next_call_forward(name, self.current_frame_seq)
            if snap is not None:
                candidates.append((snap, "function breakpoint"))

        # Data breakpoints
        for dbp in self.data_breakpoints:
            var_name = dbp.get('variableName', '')
            if not var_name:
                continue
            current_val = self._get_variable_value_at(self.current_frame_seq, var_name)
            hit = self._find_data_change_forward(var_name, current_val, self.current_frame_seq)
            if hit is not None:
                candidates.append((hit, "data breakpoint"))

        if "raised" in self.exception_filters:
            hit = self._find_next_exception_forward(
                'exception', self.current_frame_seq)
            if hit is not None:
                candidates.append((hit, "exception"))

        if "uncaught" in self.exception_filters:
            hit = self._find_next_exception_forward(
                'exception_unwind', self.current_frame_seq,
                depth_filter=0)
            if hit is not None:
                candidates.append((hit, "exception"))

        if not candidates:
            return self._navigate_to(self.last_line_seq, "end")

        best_seq, reason = min(candidates, key=lambda x: x[0])
        return self._navigate_to(best_seq, reason)

    # --- Reverse Navigation (Phase 4) ---

    def step_back(self) -> dict:
        self._require_replay()
        if self.current_frame_seq is None or self.current_frame_seq <= self.first_line_seq:
            return self._navigate_to(self.first_line_seq, "start")
        frame = self._fetch_line_before(self.current_frame_seq)
        if frame is None:
            return self._navigate_to(self.first_line_seq, "start")
        return self._navigate_to(frame.sequence_no, "step")

    def reverse_continue(self) -> dict:
        self._require_replay()
        candidates = []
        self._condition_errors = []

        for bp in self.breakpoints:
            if 'file' not in bp or 'line' not in bp:
                continue
            condition = bp.get('condition', '')
            log_message = bp.get('logMessage', '')
            # Skip log-only breakpoints for reverse continue
            if log_message:
                continue
            hit = self._find_conditional_hit_reverse(bp['file'], bp['line'],
                                                      condition, self.current_frame_seq)
            if hit is not None:
                candidates.append((hit, "breakpoint"))

        # Function breakpoints (reverse)
        for fbp in self.function_breakpoints:
            name = fbp.get('name', '')
            if not name:
                continue
            snap = self._find_next_call_reverse(name, self.current_frame_seq)
            if snap is not None:
                candidates.append((snap, "function breakpoint"))

        # Data breakpoints (reverse)
        for dbp in self.data_breakpoints:
            var_name = dbp.get('variableName', '')
            if not var_name:
                continue
            current_val = self._get_variable_value_at(self.current_frame_seq, var_name)
            hit = self._find_data_change_reverse(var_name, current_val, self.current_frame_seq)
            if hit is not None:
                candidates.append((hit, "data breakpoint"))

        if "raised" in self.exception_filters:
            hit = self._find_next_exception_reverse(
                'exception', self.current_frame_seq)
            if hit is not None:
                candidates.append((hit, "exception"))

        if "uncaught" in self.exception_filters:
            hit = self._find_next_exception_reverse(
                'exception_unwind', self.current_frame_seq,
                depth_filter=0)
            if hit is not None:
                candidates.append((hit, "exception"))

        if not candidates:
            return self._navigate_to(self.first_line_seq, "start")

        best_seq, reason = max(candidates, key=lambda x: x[0])
        return self._navigate_to(best_seq, reason)

    # --- Frame Jump Navigation (Phase 4) ---

    def goto_frame(self, target_seq: int) -> dict:
        self._require_replay()
        # 1. Validate target exists
        frame = self._fetch_frame(target_seq)
        if frame is None:
            return {"error": "frame_not_found", "target_seq": target_seq}

        # 2. Snap to nearest line event if not already one
        if frame.frame_event != 'line':
            line_fwd = self._fetch_line_after(target_seq)
            line_bwd = db.fetchone(
                "SELECT * FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'line'"
                " AND sequence_no < ?"
                " ORDER BY sequence_no DESC LIMIT 1",
                (str(self.run_id), target_seq))
            if line_fwd and line_bwd:
                # Pick the closer one
                if (target_seq - line_bwd.sequence_no) <= (line_fwd.sequence_no - target_seq):
                    target_seq = line_bwd.sequence_no
                else:
                    target_seq = line_fwd.sequence_no
            elif line_bwd:
                target_seq = line_bwd.sequence_no
            elif line_fwd:
                target_seq = line_fwd.sequence_no
            else:
                return {"error": "no_line_event", "target_seq": target_seq}

        # 3. Navigate (ReplayController handles cold vs warm)
        replay_result = self.replay_controller.goto_frame(self.run_id, target_seq)
        if replay_result.get("error"):
            return replay_result

        # 4. Rebuild stack and update state
        self.current_frame_seq = target_seq
        self.current_stack = self._build_stack_at(target_seq)

        # 5. Identify thread and cache at checkpoint boundaries
        target_frame = self._fetch_frame(target_seq)
        if target_frame:
            self.current_thread_id = target_frame.thread_id

        is_checkpoint = db.fetchone(
            "SELECT 1 FROM checkpoint WHERE run_id = ? AND sequence_no = ? LIMIT 1",
            (str(self.run_id), target_seq)) is not None
        if is_checkpoint:
            cache_thread = target_frame.thread_id if target_frame else (self.current_thread_id or 0)
            self._stack_cache[(target_seq, cache_thread)] = [e.copy() for e in self.current_stack]

        # 6. Return result
        if target_frame:
            return {
                "seq": target_seq,
                "file": target_frame.filename,
                "line": target_frame.line_no,
                "function_name": target_frame.function_name,
                "thread_id": target_frame.thread_id,
                "reason": "goto",
            }
        return {"seq": target_seq, "reason": "goto"}

    def goto_targets(self, filename: str, line: int) -> list[dict]:
        self._require_replay()
        filename = os.path.realpath(filename)
        rows = db.fetchdicts(
            "SELECT sequence_no, function_name FROM executionframes"
            " WHERE run_id = ? AND filename = ? AND line_no = ?"
            " AND frame_event = 'line'"
            " ORDER BY sequence_no LIMIT 1000",
            (str(self.run_id), filename, line))
        return [{"seq": r["sequence_no"], "function_name": r["function_name"]} for r in rows]

    def restart_frame(self, frame_seq: int) -> dict:
        self._require_replay()
        frame = self._fetch_frame(frame_seq)
        if frame is None:
            return {"error": "frame_not_found"}
        depth = frame.call_depth
        frame_thread = frame.thread_id
        call_event = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'call'"
            " AND call_depth = ? AND thread_id = ?"
            " AND sequence_no <= ?"
            " ORDER BY sequence_no DESC LIMIT 1",
            (str(self.run_id), depth, frame_thread, frame_seq))
        if call_event is None:
            return {"error": "call_event_not_found"}
        first_line = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND call_depth = ? AND thread_id = ?"
            " AND sequence_no > ?"
            " ORDER BY sequence_no LIMIT 1",
            (str(self.run_id), depth, frame_thread, call_event.sequence_no))
        if first_line is None:
            return {"error": "no_line_in_frame"}
        return self.goto_frame(first_line.sequence_no)

    # --- Query ---

    def get_stack_at(self, seq: int) -> list[dict]:
        self._require_replay()
        if seq == self.current_frame_seq and self.current_stack:
            return self.current_stack
        return self._build_stack_at(seq)

    def get_variables_at(self, seq: int) -> list[dict]:
        self._require_replay()
        frame = self._fetch_frame(seq)
        if frame is None or not frame.locals_snapshot:
            return []
        try:
            locals_data = json.loads(frame.locals_snapshot)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse locals at seq %d: %s", seq, e)
            return [{
                "name": "<parse error>",
                "value": f"Locals recorded but could not be deserialized: {e}",
                "type": "error",
                "variablesReference": 0,
            }]
        variables = []
        for name, value in locals_data.items():
            ref = 0
            if isinstance(value, dict) and '__type__' in value:
                ref = self._encode_var_ref(seq, name)
            variables.append({
                "name": name,
                "value": _format_value(value),
                "type": _infer_type(value),
                "variablesReference": ref,
            })
        return variables

    def get_variable_children(self, reference: int) -> list[dict]:
        """Get children of an expandable variable by cache reference ID.

        Requires a prior get_variables_at() call to populate the cache.
        For direct access without cache, use get_variable_children_by_name().
        """
        self._require_replay()
        decoded = self._decode_var_ref(reference)
        if decoded is None:
            return []
        seq, name = decoded
        return self._extract_children(seq, name)

    def get_variable_children_by_name(self, seq: int, variable_name: str) -> list[dict]:
        """Get children of an expandable variable by frame sequence and name.

        Unlike get_variable_children(), this does not require a prior
        get_variables_at() call — it goes directly to the frame's locals.
        """
        self._require_replay()
        return self._extract_children(seq, variable_name)

    def _extract_children(self, seq: int, name: str) -> list[dict]:
        frame = self._fetch_frame(seq)
        if frame is None or not frame.locals_snapshot:
            return []
        try:
            locals_data = json.loads(frame.locals_snapshot)
        except (json.JSONDecodeError, TypeError):
            return []
        value = locals_data.get(name)
        if not isinstance(value, dict) or '__children__' not in value:
            return []
        result = []
        for child in value['__children__']:
            result.append({
                "name": str(child.get('key', '')),
                "value": str(child.get('value', '')),
                "type": child.get('type', 'str'),
                "variablesReference": 0,
            })
        return result

    def _encode_var_ref(self, seq: int, name: str) -> int:
        ref = len(self._var_ref_cache) + 1000
        self._var_ref_cache[ref] = (seq, name)
        return ref

    def _decode_var_ref(self, ref: int):
        return self._var_ref_cache.get(ref)

    def evaluate_at(self, seq: int, expression: str, context: str) -> dict:
        self._require_replay()
        frame = self._fetch_frame(seq)
        if frame is None or not frame.locals_snapshot:
            if context == "repl":
                return {"result": "<no locals at current position>"}
            return {"result": "<not available>"}
        try:
            locals_data = json.loads(frame.locals_snapshot)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse locals at seq %d: %s", seq, e)
            return {"result": "<not available>"}

        # Fast path: simple variable lookup (all contexts including repl)
        if expression in locals_data:
            val = locals_data[expression]
            return {"result": _format_value(val), "type": _infer_type(val)}

        # Build eval locals from recorded repr strings
        eval_locals = {}
        for name, value in locals_data.items():
            eval_locals[name] = _parse_repr_value(value)

        # Eval with restricted builtins (hover, watch, and repl)
        try:
            result = eval(expression, {"__builtins__": SAFE_BUILTINS}, eval_locals)
            return {"result": str(result), "type": type(result).__name__}
        except Exception:
            if context == "repl":
                return {"result": f"Error: cannot evaluate '{expression}' against recorded locals"}
            return {"result": "<not available>"}

    # --- Phase 6: CodeLens, Call History ---

    def get_traced_files(self) -> list[str]:
        self._require_replay()
        rows = db.fetchall(
            "SELECT DISTINCT filename FROM executionframes WHERE run_id = ?",
            (str(self.run_id),))
        return [row.filename for row in rows]

    def get_execution_stats(self, filename: str = "") -> list[dict]:
        self._require_replay()
        if filename:
            filename = os.path.realpath(filename)
            rows = db.fetchdicts(
                "SELECT function_name,"
                " SUM(CASE WHEN frame_event = 'call' THEN 1 ELSE 0 END) AS call_count,"
                " SUM(CASE WHEN frame_event = 'exception_unwind' THEN 1 ELSE 0 END) AS exception_count,"
                " MIN(CASE WHEN frame_event = 'call' THEN sequence_no END) AS first_call_seq,"
                " COALESCE("
                "   MIN(CASE WHEN frame_event = 'call' THEN NULLIF(line_no, -1) END),"
                "   MIN(CASE WHEN frame_event = 'line' THEN line_no END)"
                " ) AS def_line"
                " FROM executionframes"
                " WHERE run_id = ? AND filename = ?"
                " GROUP BY function_name",
                (str(self.run_id), filename))
        else:
            rows = db.fetchdicts(
                "SELECT function_name,"
                " SUM(CASE WHEN frame_event = 'call' THEN 1 ELSE 0 END) AS call_count,"
                " SUM(CASE WHEN frame_event = 'exception_unwind' THEN 1 ELSE 0 END) AS exception_count,"
                " MIN(CASE WHEN frame_event = 'call' THEN sequence_no END) AS first_call_seq,"
                " COALESCE("
                "   MIN(CASE WHEN frame_event = 'call' THEN NULLIF(line_no, -1) END),"
                "   MIN(CASE WHEN frame_event = 'line' THEN line_no END)"
                " ) AS def_line"
                " FROM executionframes"
                " WHERE run_id = ?"
                " GROUP BY function_name",
                (str(self.run_id),))

        return [{
            'functionName': r['function_name'],
            'callCount': r['call_count'] or 0,
            'exceptionCount': r['exception_count'] or 0,
            'firstCallSeq': r['first_call_seq'],
            'defLine': r['def_line'] or 0,
        } for r in rows if r['call_count']]

    def get_call_children(self, parent_call_seq=None, parent_return_seq=None) -> list[dict]:
        self._require_replay()
        run_id = str(self.run_id)

        if parent_call_seq is None:
            target_depth = 0
            range_sql = " AND sequence_no >= 0"
            range_params = ()
        else:
            parent = self._fetch_frame(parent_call_seq)
            if not parent:
                return []
            target_depth = parent.call_depth + 1
            if parent_return_seq is not None:
                range_sql = " AND sequence_no > ? AND sequence_no < ?"
                range_params = (parent_call_seq, parent_return_seq)
            else:
                range_sql = " AND sequence_no > ?"
                range_params = (parent_call_seq,)

        events = db.fetchall(
            "SELECT * FROM executionframes"
            " WHERE run_id = ?"
            " AND frame_event IN ('call', 'return', 'exception_unwind')"
            " AND call_depth = ?"
            + range_sql +
            " ORDER BY sequence_no",
            (run_id, target_depth) + range_params)

        # Python 3.12 fires PyTrace_RETURN with Py_None (not NULL) during
        # exception propagation, causing a spurious 'return' event before
        # the eval hook's 'exception_unwind'. Filter these out.
        filtered = []
        for j, ev in enumerate(events):
            if (ev.frame_event == 'return' and
                    j + 1 < len(events) and
                    events[j + 1].frame_event == 'exception_unwind' and
                    events[j + 1].function_name == ev.function_name):
                continue
            filtered.append(ev)
        events = filtered

        results = []
        i = 0
        while i < len(events):
            ev = events[i]
            if ev.frame_event != 'call':
                i += 1
                continue
            return_ev = None
            if (i + 1 < len(events) and
                    events[i + 1].frame_event in ('return', 'exception_unwind')):
                return_ev = events[i + 1]
                i += 2
            else:
                i += 1
            results.append({
                'callSeq': ev.sequence_no,
                'returnSeq': return_ev.sequence_no if return_ev else None,
                'functionName': ev.function_name,
                'filename': ev.filename,
                'line': ev.line_no,
                'depth': ev.call_depth,
                'hasException': (return_ev.frame_event == 'exception_unwind'
                                 if return_ev else False),
                'isComplete': return_ev is not None,
            })
        return results

    # --- Phase 10B: Variable History ---

    def get_variable_history(self, variable_name: str, start_seq: int, end_seq: int,
                             max_points: int = 500) -> list[dict]:
        self._require_replay()
        frames = db.iterate(
            "SELECT sequence_no, line_no, filename, function_name, locals_snapshot"
            " FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND sequence_no >= ? AND sequence_no <= ?"
            " AND locals_snapshot LIKE '%' || ? || '%'"
            " ORDER BY sequence_no",
            (str(self.run_id), start_seq, end_seq, f'"{variable_name}"'))

        result = []
        last_value = object()
        for frame in frames:
            if not frame.locals_snapshot:
                continue
            try:
                locals_data = json.loads(frame.locals_snapshot)
            except (json.JSONDecodeError, TypeError):
                continue
            if variable_name not in locals_data:
                continue
            value = _format_value(locals_data[variable_name])
            if value == last_value:
                continue
            last_value = value
            result.append({
                'seq': frame.sequence_no,
                'value': value,
                'line': frame.line_no,
                'filename': frame.filename,
                'functionName': frame.function_name,
            })
            if len(result) >= max_points:
                break
        return result

    # --- Breakpoint Verification (P1-2 + P1-5) ---

    def verify_breakpoints(self, breakpoints: list[dict]) -> list[dict]:
        """Verify breakpoints: check line exists in recording, validate condition syntax."""
        results = []
        for bp in breakpoints:
            file = bp.get('file', '')
            line = bp.get('line', 0)
            condition = bp.get('condition', '')
            verified = True
            message = ''

            if file and line and self.state == "replay":
                real_file = os.path.realpath(file)
                exists = db.fetchone(
                    "SELECT 1 FROM executionframes"
                    " WHERE run_id = ? AND filename = ? AND line_no = ?"
                    " AND frame_event = 'line' LIMIT 1",
                    (str(self.run_id), real_file, line)) is not None
                if not exists:
                    file_exists = db.fetchone(
                        "SELECT 1 FROM executionframes"
                        " WHERE run_id = ? AND filename = ? LIMIT 1",
                        (str(self.run_id), real_file)) is not None
                    if not file_exists:
                        verified = False
                        message = f"File not in recording: {os.path.basename(file)}"
                    else:
                        verified = False
                        message = f"Line {line} was not executed in the recording"

            if condition and condition.strip():
                try:
                    compile(condition, '<breakpoint_condition>', 'eval')
                except SyntaxError as e:
                    verified = False
                    message = f"Invalid condition: {e.msg}"

            results.append({
                'verified': verified,
                'message': message,
                'file': file,
                'line': line,
            })
        return results

    def get_condition_errors(self) -> list[dict]:
        """Return condition eval errors from the last continue/reverse_continue.
        Each entry: {seq, condition, error}."""
        return list(self._condition_errors)

    # --- Internal helpers ---

    def _evaluate_condition(self, condition: str, seq: int) -> bool:
        if not condition or not condition.strip():
            return True
        frame = self._fetch_frame(seq)
        if frame is None:
            return True
        if not frame.locals_snapshot:
            return False  # can't evaluate — skip (locals may be sampled out)
        try:
            locals_data = json.loads(frame.locals_snapshot)
        except (json.JSONDecodeError, TypeError):
            return True
        eval_locals = {}
        for name, value in locals_data.items():
            eval_locals[name] = _parse_repr_value(value)
        try:
            return bool(eval(condition, {"__builtins__": SAFE_BUILTINS}, eval_locals))
        except Exception as e:
            logger.warning("Condition eval error at seq %d: %s", seq, e)
            self._condition_errors.append({
                "seq": seq, "condition": condition, "error": str(e)
            })
            return False

    def _find_conditional_hit_forward(self, filename: str, line: int,
                                       condition: str, after_seq: int,
                                       hit_condition: str = '',
                                       log_message: str = '') -> int | None:
        bp_key = (filename, line)

        if not condition or not condition.strip():
            # Fast path: no expression condition — use loop (not recursion)
            # to avoid stack overflow with large hit conditions
            cursor = after_seq
            while True:
                hit = db.fetchone(
                    "SELECT sequence_no FROM executionframes"
                    " WHERE run_id = ? AND filename = ? AND line_no = ?"
                    " AND frame_event = 'line' AND sequence_no > ?"
                    " ORDER BY sequence_no LIMIT 1",
                    (str(self.run_id), filename, line, cursor))
                if hit is None:
                    return None
                # Check hit condition
                if hit_condition:
                    self._bp_hit_counts[bp_key] = self._bp_hit_counts.get(bp_key, 0) + 1
                    if not self._check_hit_condition(hit_condition, self._bp_hit_counts[bp_key]):
                        cursor = hit.sequence_no
                        continue
                # Handle log points
                if log_message:
                    msg = self._format_log_message(log_message, hit.sequence_no)
                    self._log_messages.append(msg)
                    # Don't stop, but return the seq so caller knows we hit something
                    return hit.sequence_no
                return hit.sequence_no

        cursor = after_seq
        for _ in range(MAX_CONDITIONAL_SEARCH):
            hit = db.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND filename = ? AND line_no = ?"
                " AND frame_event = 'line' AND sequence_no > ?"
                " ORDER BY sequence_no LIMIT 1",
                (str(self.run_id), filename, line, cursor))
            if hit is None:
                return None
            if self._evaluate_condition(condition, hit.sequence_no):
                # Check hit condition
                if hit_condition:
                    self._bp_hit_counts[bp_key] = self._bp_hit_counts.get(bp_key, 0) + 1
                    if not self._check_hit_condition(hit_condition, self._bp_hit_counts[bp_key]):
                        cursor = hit.sequence_no
                        continue
                # Handle log points
                if log_message:
                    msg = self._format_log_message(log_message, hit.sequence_no)
                    self._log_messages.append(msg)
                    return hit.sequence_no
                return hit.sequence_no
            cursor = hit.sequence_no
        logger.warning("Conditional breakpoint search exhausted %d iterations (forward, %s:%d)",
                       MAX_CONDITIONAL_SEARCH, filename, line)
        return None

    def _find_conditional_hit_reverse(self, filename: str, line: int,
                                       condition: str, before_seq: int) -> int | None:
        if not condition or not condition.strip():
            hit = db.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND filename = ? AND line_no = ?"
                " AND frame_event = 'line' AND sequence_no < ?"
                " ORDER BY sequence_no DESC LIMIT 1",
                (str(self.run_id), filename, line, before_seq))
            return hit.sequence_no if hit else None

        cursor = before_seq
        for _ in range(MAX_CONDITIONAL_SEARCH):
            hit = db.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND filename = ? AND line_no = ?"
                " AND frame_event = 'line' AND sequence_no < ?"
                " ORDER BY sequence_no DESC LIMIT 1",
                (str(self.run_id), filename, line, cursor))
            if hit is None:
                return None
            if self._evaluate_condition(condition, hit.sequence_no):
                return hit.sequence_no
            cursor = hit.sequence_no
        logger.warning("Conditional breakpoint search exhausted %d iterations (reverse, %s:%d)",
                       MAX_CONDITIONAL_SEARCH, filename, line)
        return None

    def _check_hit_condition(self, hit_condition: str, count: int) -> bool:
        """Check if hit count satisfies the hit condition expression.
        Supports: plain number (==), >=N, >N, <=N, <N, ==N, %N (modulo)."""
        hit_condition = hit_condition.strip()
        if not hit_condition:
            return True
        try:
            if hit_condition.startswith('>='):
                return count >= int(hit_condition[2:].strip())
            elif hit_condition.startswith('>'):
                return count > int(hit_condition[1:].strip())
            elif hit_condition.startswith('<='):
                return count <= int(hit_condition[2:].strip())
            elif hit_condition.startswith('<'):
                return count < int(hit_condition[1:].strip())
            elif hit_condition.startswith('=='):
                return count == int(hit_condition[2:].strip())
            elif hit_condition.startswith('%'):
                mod = int(hit_condition[1:].strip())
                return mod > 0 and count % mod == 0
            else:
                return count == int(hit_condition)
        except (ValueError, ZeroDivisionError):
            return True

    def _format_log_message(self, template: str, seq: int) -> str:
        """Format a DAP log message template using {expression} syntax."""
        import re
        frame = self._fetch_frame(seq)
        if frame is None or not frame.locals_snapshot:
            return template
        try:
            locals_data = json.loads(frame.locals_snapshot)
        except (json.JSONDecodeError, TypeError):
            return template

        def _replace(m):
            expr = m.group(1)
            if expr in locals_data:
                return _format_value(locals_data[expr])
            return m.group(0)  # Leave as-is if not found

        return re.sub(r'\{([^}]+)\}', _replace, template)

    def _get_variable_value_at(self, seq: int, var_name: str) -> str | None:
        """Get the value of a variable at a specific frame sequence."""
        frame = self._fetch_frame(seq)
        if frame is None or not frame.locals_snapshot:
            return None
        try:
            locals_data = json.loads(frame.locals_snapshot)
        except (json.JSONDecodeError, TypeError):
            return None
        if var_name not in locals_data:
            return None
        return _format_value(locals_data[var_name])

    def _find_data_change_forward(self, var_name: str, current_val: str | None,
                                   after_seq: int) -> int | None:
        """Find the next frame where var_name has a different value than current_val."""
        frames = db.iterate(
            "SELECT sequence_no, locals_snapshot FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND sequence_no > ?"
            " AND locals_snapshot LIKE '%' || ? || '%'"
            " ORDER BY sequence_no LIMIT 10000",
            (str(self.run_id), after_seq, f'"{var_name}"'))

        for frame in frames:
            if not frame.locals_snapshot:
                continue
            try:
                locals_data = json.loads(frame.locals_snapshot)
            except (json.JSONDecodeError, TypeError):
                continue
            if var_name not in locals_data:
                if current_val is not None:
                    return frame.sequence_no
                continue
            val = _format_value(locals_data[var_name])
            if val != current_val:
                return frame.sequence_no
        return None

    def _find_data_change_reverse(self, var_name: str, current_val: str | None,
                                   before_seq: int) -> int | None:
        """Find the previous frame where var_name had a different value than current_val."""
        frames = db.iterate(
            "SELECT sequence_no, locals_snapshot FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND sequence_no < ?"
            " AND locals_snapshot LIKE '%' || ? || '%'"
            " ORDER BY sequence_no DESC LIMIT 10000",
            (str(self.run_id), before_seq, f'"{var_name}"'))

        for frame in frames:
            if not frame.locals_snapshot:
                continue
            try:
                locals_data = json.loads(frame.locals_snapshot)
            except (json.JSONDecodeError, TypeError):
                continue
            if var_name not in locals_data:
                if current_val is not None:
                    return frame.sequence_no
                continue
            val = _format_value(locals_data[var_name])
            if val != current_val:
                return frame.sequence_no
        return None

    def _snap_to_line(self, seq: int) -> int:
        """Snap an exception/call/return event to the nearest line event.
        Prefers the preceding line event (same line, just before exception)."""
        line_bwd = db.fetchone(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND sequence_no <= ?"
            " ORDER BY sequence_no DESC LIMIT 1",
            (str(self.run_id), seq))
        if line_bwd:
            return line_bwd.sequence_no
        line_fwd = db.fetchone(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND sequence_no > ?"
            " ORDER BY sequence_no LIMIT 1",
            (str(self.run_id), seq))
        return line_fwd.sequence_no if line_fwd else seq

    def _find_next_exception_forward(self, event_type: str,
                                      current_seq: int,
                                      depth_filter: int | None = None) -> int | None:
        """Find the next exception event after current_seq, snapped to a line.

        Handles the snap-to-line aliasing problem: if the snapped position
        equals current_seq, skip that exception and find the next one.
        """
        search_after = current_seq
        while True:
            depth_sql = ""
            depth_params = ()
            if depth_filter is not None:
                depth_sql = " AND call_depth = ?"
                depth_params = (depth_filter,)
            hit = db.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND frame_event = ?"
                " AND sequence_no > ?"
                + depth_sql +
                " ORDER BY sequence_no LIMIT 1",
                (str(self.run_id), event_type, search_after) + depth_params)
            if hit is None:
                return None
            snap = self._snap_to_line(hit.sequence_no)
            if snap > current_seq:
                return snap
            # Snapped position didn't advance past current — skip this
            # exception's raw seq and search for the next one.
            search_after = hit.sequence_no

    def _find_next_exception_reverse(self, event_type: str,
                                      current_seq: int,
                                      depth_filter: int | None = None) -> int | None:
        """Find the previous exception event before current_seq, snapped to a line.

        Handles the snap-to-line aliasing problem: if the snapped position
        equals current_seq, skip that exception and find the previous one.
        """
        search_before = current_seq
        while True:
            depth_sql = ""
            depth_params = ()
            if depth_filter is not None:
                depth_sql = " AND call_depth = ?"
                depth_params = (depth_filter,)
            hit = db.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND frame_event = ?"
                " AND sequence_no < ?"
                + depth_sql +
                " ORDER BY sequence_no DESC LIMIT 1",
                (str(self.run_id), event_type, search_before) + depth_params)
            if hit is None:
                return None
            snap = self._snap_to_line(hit.sequence_no)
            if snap < current_seq:
                return snap
            # Snapped position didn't retreat past current — skip this
            # exception's raw seq and search for the previous one.
            search_before = hit.sequence_no

    def _find_next_call_forward(self, name: str, current_seq: int) -> int | None:
        """Find the next function call matching name after current_seq, snapped to a line.

        Handles the snap-to-line aliasing problem for call events.
        """
        search_after = current_seq
        while True:
            hit = db.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'call'"
                " AND function_name LIKE '%' || ? || '%'"
                " AND sequence_no > ?"
                " ORDER BY sequence_no LIMIT 1",
                (str(self.run_id), name, search_after))
            if not hit:
                return None
            snap = self._snap_to_line(hit.sequence_no)
            if snap > current_seq:
                return snap
            search_after = hit.sequence_no

    def _find_next_call_reverse(self, name: str, current_seq: int) -> int | None:
        """Find the previous function call matching name before current_seq, snapped to a line.

        Handles the snap-to-line aliasing problem for call events.
        """
        search_before = current_seq
        while True:
            hit = db.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'call'"
                " AND function_name LIKE '%' || ? || '%'"
                " AND sequence_no < ?"
                " ORDER BY sequence_no DESC LIMIT 1",
                (str(self.run_id), name, search_before))
            if not hit:
                return None
            snap = self._snap_to_line(hit.sequence_no)
            if snap < current_seq:
                return snap
            search_before = hit.sequence_no

    def _get_current_frame(self):
        return self._fetch_frame(self.current_frame_seq)

    def _navigate_to(self, seq: int, reason: str) -> dict:
        old_seq = self.current_frame_seq
        self.current_frame_seq = seq

        # Resolve target thread before updating stack — cross-thread
        # navigation must rebuild from scratch (incremental update would
        # scan the wrong thread's events).
        frame = self._fetch_frame(seq)
        target_thread = frame.thread_id if frame else self.current_thread_id

        if target_thread != self.current_thread_id:
            self.current_thread_id = target_thread
            self.current_stack = self._build_stack_at(seq)
        else:
            self._update_stack(old_seq, seq)

        if frame:
            self.current_thread_id = frame.thread_id
            return {
                "seq": seq,
                "file": frame.filename,
                "line": frame.line_no,
                "function_name": frame.function_name,
                "thread_id": frame.thread_id,
                "reason": reason,
            }
        return {"seq": seq, "reason": reason}

    def _update_stack(self, old_seq: int, new_seq: int):
        if old_seq is None or old_seq == new_seq:
            return

        if new_seq > old_seq:
            # Forward: scan events between old and new, push/pop
            # Stack is DAP order (deepest-first at index 0)
            if self.current_thread_id is not None:
                thread_sql = " AND thread_id = ?"
                thread_params = (self.current_thread_id,)
            else:
                thread_sql = " AND sequence_no >= 0"
                thread_params = ()
            events = db.fetchall(
                "SELECT * FROM executionframes"
                " WHERE run_id = ?"
                + thread_sql +
                " AND sequence_no > ? AND sequence_no <= ?"
                " ORDER BY sequence_no",
                (str(self.run_id),) + thread_params + (old_seq, new_seq))
            for ev in events:
                if ev.frame_event == 'call':
                    self.current_stack.insert(0, self._frame_to_stack_entry(ev))
                elif ev.frame_event in ('return', 'exception_unwind'):
                    if self.current_stack and self.current_stack[0]['depth'] == ev.call_depth:
                        self.current_stack.pop(0)
                    elif self.current_stack and self.current_stack[0]['depth'] > ev.call_depth:
                        while self.current_stack and self.current_stack[0]['depth'] > ev.call_depth:
                            self.current_stack.pop(0)
                        if self.current_stack and self.current_stack[0]['depth'] == ev.call_depth:
                            self.current_stack.pop(0)
                elif ev.frame_event == 'line':
                    if self.current_stack and self.current_stack[0]['depth'] == ev.call_depth:
                        self.current_stack[0] = self._frame_to_stack_entry(ev)
        else:
            # Backward navigation — full rebuild (incremental reverse scan
            # can't correctly update outer stack entries across boundaries)
            self.current_stack = self._build_stack_at(new_seq)

    def _build_stack_at(self, seq: int) -> list[dict]:
        # Determine target thread for this position
        target = self._fetch_frame(seq)
        target_thread = target.thread_id if target else (self.current_thread_id or 0)

        # Find nearest cached stack <= seq for the same thread
        cached_seqs = [s for (s, t) in self._stack_cache if s <= seq and t == target_thread]
        if cached_seqs:
            start_seq = max(cached_seqs)
            stack = [entry.copy() for entry in self._stack_cache[(start_seq, target_thread)]]
            # Cached stacks are DAP order (deepest-first); scan uses call-stack order
            stack.reverse()
            lower_sql = " AND sequence_no > ?"
            lower_params = (start_seq,)
        else:
            stack = []
            lower_sql = " AND sequence_no >= 0"
            lower_params = ()

        events = db.fetchall(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND thread_id = ?"
            + lower_sql +
            " AND sequence_no <= ?"
            " ORDER BY sequence_no",
            (str(self.run_id), target_thread) + lower_params + (seq,))
        for ev in events:
            if ev.frame_event == 'call':
                stack.append(self._frame_to_stack_entry(ev))
            elif ev.frame_event in ('return', 'exception_unwind'):
                if stack and stack[-1]['depth'] == ev.call_depth:
                    stack.pop()
                elif stack and stack[-1]['depth'] > ev.call_depth:
                    # Orphaned return — pop entries deeper than this return
                    while stack and stack[-1]['depth'] > ev.call_depth:
                        stack.pop()
                    if stack and stack[-1]['depth'] == ev.call_depth:
                        stack.pop()
            elif ev.frame_event == 'line':
                if stack and stack[-1]['depth'] == ev.call_depth:
                    stack[-1] = self._frame_to_stack_entry(ev)

        stack.reverse()
        return stack

    def _frame_to_stack_entry(self, frame) -> dict:
        return {
            "seq": frame.sequence_no,
            "name": frame.function_name,
            "file": frame.filename,
            "line": frame.line_no,
            "depth": frame.call_depth,
        }


def _parse_repr_value(value):
    """Safely convert a repr string back to a Python literal.
    Uses ast.literal_eval (safe — only parses literals, no code execution).
    Falls back to the raw string on failure."""
    import ast
    if isinstance(value, dict) and '__type__' in value:
        s = _format_value(value)
    else:
        s = str(value)
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return s


def _format_value(value) -> str:
    if isinstance(value, dict) and '__type__' in value:
        t = value['__type__']
        length = value.get('__len__', '?')
        rep = value.get('__repr__', f'{t}(...)')
        return rep
    return str(value)


def _infer_type(value) -> str:
    if isinstance(value, dict) and '__type__' in value:
        return value['__type__']
    s = str(value)
    if s in ('True', 'False'):
        return 'bool'
    if s == 'None':
        return 'NoneType'
    try:
        int(s)
        return 'int'
    except (ValueError, TypeError):
        pass
    try:
        float(s)
        return 'float'
    except (ValueError, TypeError):
        pass
    if s.startswith('['):
        return 'list'
    if s.startswith('{'):
        return 'dict'
    if s.startswith('('):
        return 'tuple'
    return 'str'
