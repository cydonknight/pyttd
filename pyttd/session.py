import json
import os
from pyttd.models.frames import ExecutionFrames
from pyttd.replay import ReplayController


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
        self.current_stack = []    # [{seq, name, file, line, depth}, ...]
        self.first_line_seq = None
        self.last_line_seq = None
        self.replay_controller = ReplayController()
        self._stack_cache = {}  # (seq, thread_id) -> stack_snapshot (DAP order)
        self._var_ref_cache = {}  # ref_id -> (seq, name) for expandable variables
        self.current_thread_id = None
        self.known_threads = {}  # {thread_id: "Thread Name"}

    def enter_replay(self, run_id, first_line_seq: int):
        self.run_id = run_id
        self.state = "replay"
        self.current_frame_seq = first_line_seq

        # Cache boundary seqs
        self.first_line_seq = first_line_seq
        last = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                .where((ExecutionFrames.run_id == run_id) &
                       (ExecutionFrames.frame_event == 'line'))
                .order_by(ExecutionFrames.sequence_no.desc())
                .limit(1).first())
        self.last_line_seq = last.sequence_no if last else first_line_seq

        # Identify main thread from the first recorded event (sequence_no 0)
        first_event = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.sequence_no == 0))
        main_thread_id = first_event.thread_id if first_event else None

        # Discover threads, labeling the main thread correctly
        thread_rows = list(ExecutionFrames.select(ExecutionFrames.thread_id)
            .where(ExecutionFrames.run_id == run_id)
            .distinct())
        self.known_threads = {}
        for row in thread_rows:
            if row.thread_id == main_thread_id:
                self.known_threads[row.thread_id] = "Main Thread"
            else:
                self.known_threads[row.thread_id] = f"Thread {row.thread_id}"

        # Set current thread from first line event
        first = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.sequence_no == first_line_seq))
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
                exists = ExecutionFrames.select().where(
                    (ExecutionFrames.run_id == self.run_id) &
                    (ExecutionFrames.frame_event == 'call') &
                    (ExecutionFrames.function_name.contains(name))
                ).limit(1).exists()
                if not exists:
                    results.append({'verified': False, 'message': f"Function '{name}' not found in recording"})
                else:
                    results.append({'verified': True})
            else:
                results.append({'verified': True})
        return results

    def get_threads(self) -> list[dict]:
        return [{"id": tid, "name": name} for tid, name in self.known_threads.items()]

    # --- Forward Navigation ---

    def step_into(self) -> dict:
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == self.run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.sequence_no > self.current_frame_seq))
                 .order_by(ExecutionFrames.sequence_no)
                 .limit(1).first())
        if frame is None:
            return self._navigate_to(self.last_line_seq, "end")
        return self._navigate_to(frame.sequence_no, "step")

    def step_over(self) -> dict:
        current = self._get_current_frame()
        if current is None:
            return self._navigate_to(self.last_line_seq, "end")
        current_depth = current.call_depth
        current_thread = current.thread_id
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == self.run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.call_depth <= current_depth) &
                        (ExecutionFrames.thread_id == current_thread) &
                        (ExecutionFrames.sequence_no > self.current_frame_seq))
                 .order_by(ExecutionFrames.sequence_no)
                 .limit(1).first())
        if frame is None:
            return self._navigate_to(self.last_line_seq, "end")
        return self._navigate_to(frame.sequence_no, "step")

    def step_out(self) -> dict:
        current = self._get_current_frame()
        if current is None:
            return self._navigate_to(self.last_line_seq, "end")
        current_depth = current.call_depth
        current_thread = current.thread_id

        if current_depth == 0:
            return self._navigate_to(self.last_line_seq, "end")

        exit_event = (ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == self.run_id) &
                             (ExecutionFrames.frame_event.in_(['return', 'exception_unwind'])) &
                             (ExecutionFrames.call_depth == current_depth) &
                             (ExecutionFrames.thread_id == current_thread) &
                             (ExecutionFrames.sequence_no > self.current_frame_seq))
                      .order_by(ExecutionFrames.sequence_no)
                      .first())
        if exit_event is None:
            return self._navigate_to(self.last_line_seq, "end")

        parent_line = (ExecutionFrames.select()
                       .where((ExecutionFrames.run_id == self.run_id) &
                              (ExecutionFrames.frame_event == 'line') &
                              (ExecutionFrames.call_depth == current_depth - 1) &
                              (ExecutionFrames.thread_id == current_thread) &
                              (ExecutionFrames.sequence_no > exit_event.sequence_no))
                       .order_by(ExecutionFrames.sequence_no)
                       .first())

        if parent_line is None and exit_event.frame_event == 'exception_unwind':
            parent_line = (ExecutionFrames.select()
                           .where((ExecutionFrames.run_id == self.run_id) &
                                  (ExecutionFrames.frame_event == 'line') &
                                  (ExecutionFrames.call_depth < current_depth) &
                                  (ExecutionFrames.thread_id == current_thread) &
                                  (ExecutionFrames.sequence_no > exit_event.sequence_no))
                           .order_by(ExecutionFrames.sequence_no)
                           .first())

        if parent_line is None:
            return self._navigate_to(self.last_line_seq, "end")
        return self._navigate_to(parent_line.sequence_no, "step")

    def continue_forward(self) -> dict:
        candidates = []
        self._log_messages = []

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
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.frame_event == 'call') &
                          (ExecutionFrames.function_name.contains(name)) &
                          (ExecutionFrames.sequence_no > self.current_frame_seq))
                   .order_by(ExecutionFrames.sequence_no)
                   .limit(1).first())
            if hit:
                snap = self._snap_to_line(hit.sequence_no)
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
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.frame_event == 'exception') &
                          (ExecutionFrames.sequence_no > self.current_frame_seq))
                   .order_by(ExecutionFrames.sequence_no)
                   .limit(1).first())
            if hit:
                snap = self._snap_to_line(hit.sequence_no)
                candidates.append((snap, "exception"))

        if "uncaught" in self.exception_filters:
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.frame_event == 'exception_unwind') &
                          (ExecutionFrames.call_depth == 0) &
                          (ExecutionFrames.sequence_no > self.current_frame_seq))
                   .order_by(ExecutionFrames.sequence_no)
                   .limit(1).first())
            if hit:
                snap = self._snap_to_line(hit.sequence_no)
                candidates.append((snap, "exception"))

        if not candidates:
            return self._navigate_to(self.last_line_seq, "end")

        best_seq, reason = min(candidates, key=lambda x: x[0])
        return self._navigate_to(best_seq, reason)

    # --- Reverse Navigation (Phase 4) ---

    def step_back(self) -> dict:
        if self.current_frame_seq is None or self.current_frame_seq <= self.first_line_seq:
            return self._navigate_to(self.first_line_seq, "start")
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == self.run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.sequence_no < self.current_frame_seq))
                 .order_by(ExecutionFrames.sequence_no.desc())
                 .limit(1).first())
        if frame is None:
            return self._navigate_to(self.first_line_seq, "start")
        return self._navigate_to(frame.sequence_no, "step")

    def reverse_continue(self) -> dict:
        candidates = []

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
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.frame_event == 'call') &
                          (ExecutionFrames.function_name.contains(name)) &
                          (ExecutionFrames.sequence_no < self.current_frame_seq))
                   .order_by(ExecutionFrames.sequence_no.desc())
                   .limit(1).first())
            if hit:
                snap = self._snap_to_line(hit.sequence_no)
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
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.frame_event == 'exception') &
                          (ExecutionFrames.sequence_no < self.current_frame_seq))
                   .order_by(ExecutionFrames.sequence_no.desc())
                   .limit(1).first())
            if hit:
                snap = self._snap_to_line(hit.sequence_no)
                candidates.append((snap, "exception"))

        if "uncaught" in self.exception_filters:
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.frame_event == 'exception_unwind') &
                          (ExecutionFrames.call_depth == 0) &
                          (ExecutionFrames.sequence_no < self.current_frame_seq))
                   .order_by(ExecutionFrames.sequence_no.desc())
                   .limit(1).first())
            if hit:
                snap = self._snap_to_line(hit.sequence_no)
                candidates.append((snap, "exception"))

        if not candidates:
            return self._navigate_to(self.first_line_seq, "start")

        best_seq, reason = max(candidates, key=lambda x: x[0])
        return self._navigate_to(best_seq, reason)

    # --- Frame Jump Navigation (Phase 4) ---

    def goto_frame(self, target_seq: int) -> dict:
        # 1. Validate target exists
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == target_seq))
        if frame is None:
            return {"error": "frame_not_found", "target_seq": target_seq}

        # 2. Snap to nearest line event if not already one
        if frame.frame_event != 'line':
            line_fwd = (ExecutionFrames.select()
                        .where((ExecutionFrames.run_id == self.run_id) &
                               (ExecutionFrames.frame_event == 'line') &
                               (ExecutionFrames.sequence_no > target_seq))
                        .order_by(ExecutionFrames.sequence_no).first())
            line_bwd = (ExecutionFrames.select()
                        .where((ExecutionFrames.run_id == self.run_id) &
                               (ExecutionFrames.frame_event == 'line') &
                               (ExecutionFrames.sequence_no < target_seq))
                        .order_by(ExecutionFrames.sequence_no.desc()).first())
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
        target_frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == target_seq))
        if target_frame:
            self.current_thread_id = target_frame.thread_id

        from pyttd.models.checkpoints import Checkpoint
        is_checkpoint = Checkpoint.select().where(
            (Checkpoint.run_id == self.run_id) &
            (Checkpoint.sequence_no == target_seq)
        ).exists()
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
        filename = os.path.realpath(filename)
        results = list(ExecutionFrames.select(
            ExecutionFrames.sequence_no, ExecutionFrames.function_name
        ).where(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.filename == filename) &
            (ExecutionFrames.line_no == line) &
            (ExecutionFrames.frame_event == 'line')
        ).order_by(ExecutionFrames.sequence_no).limit(1000).dicts())
        return [{"seq": r["sequence_no"], "function_name": r["function_name"]} for r in results]

    def restart_frame(self, frame_seq: int) -> dict:
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == frame_seq))
        if frame is None:
            return {"error": "frame_not_found"}
        depth = frame.call_depth
        frame_thread = frame.thread_id
        call_event = (ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == self.run_id) &
                             (ExecutionFrames.frame_event == 'call') &
                             (ExecutionFrames.call_depth == depth) &
                             (ExecutionFrames.thread_id == frame_thread) &
                             (ExecutionFrames.sequence_no <= frame_seq))
                      .order_by(ExecutionFrames.sequence_no.desc()).first())
        if call_event is None:
            return {"error": "call_event_not_found"}
        first_line = (ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == self.run_id) &
                             (ExecutionFrames.frame_event == 'line') &
                             (ExecutionFrames.call_depth == depth) &
                             (ExecutionFrames.thread_id == frame_thread) &
                             (ExecutionFrames.sequence_no > call_event.sequence_no))
                      .order_by(ExecutionFrames.sequence_no).first())
        if first_line is None:
            return {"error": "no_line_in_frame"}
        return self.goto_frame(first_line.sequence_no)

    # --- Query ---

    def get_stack_at(self, seq: int) -> list[dict]:
        if seq == self.current_frame_seq and self.current_stack:
            return self.current_stack
        return self._build_stack_at(seq)

    def get_variables_at(self, seq: int) -> list[dict]:
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == seq))
        if frame is None or not frame.locals_snapshot:
            return []
        try:
            locals_data = json.loads(frame.locals_snapshot)
        except (json.JSONDecodeError, TypeError) as e:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to parse locals at seq %d: %s", seq, e)
            return []
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
        decoded = self._decode_var_ref(reference)
        if decoded is None:
            return []
        seq, name = decoded
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == seq))
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
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == seq))
        if frame is None or not frame.locals_snapshot:
            return {"result": "<not available>"}
        try:
            locals_data = json.loads(frame.locals_snapshot)
        except (json.JSONDecodeError, TypeError) as e:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to parse locals at seq %d: %s", seq, e)
            return {"result": "<not available>"}

        if expression in locals_data:
            val = locals_data[expression]
            return {"result": str(val), "type": _infer_type(val)}

        base = expression.split('.')[0]
        if base in locals_data:
            return {"result": str(locals_data[base]), "type": _infer_type(locals_data[base])}

        return {"result": "<not available>"}

    # --- Phase 6: CodeLens, Call History ---

    def get_traced_files(self) -> list[str]:
        rows = (ExecutionFrames.select(ExecutionFrames.filename)
                .where(ExecutionFrames.run_id == self.run_id)
                .distinct())
        return [row.filename for row in rows]

    def get_execution_stats(self, filename: str) -> list[dict]:
        from peewee import fn, SQL
        rows = list(ExecutionFrames.select(
            ExecutionFrames.function_name,
            fn.SUM(SQL("CASE WHEN frame_event = 'call' THEN 1 ELSE 0 END")).alias('call_count'),
            fn.SUM(SQL("CASE WHEN frame_event = 'exception_unwind' "
                       "THEN 1 ELSE 0 END")).alias('exception_count'),
            fn.MIN(SQL("CASE WHEN frame_event = 'call' THEN sequence_no END")).alias('first_call_seq'),
            fn.MIN(SQL("CASE WHEN frame_event = 'call' THEN line_no END")).alias('def_line'),
        ).where(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.filename == filename)
        ).group_by(ExecutionFrames.function_name).dicts())

        return [{
            'functionName': r['function_name'],
            'callCount': r['call_count'] or 0,
            'exceptionCount': r['exception_count'] or 0,
            'firstCallSeq': r['first_call_seq'],
            'defLine': r['def_line'],
        } for r in rows if r['call_count']]

    def get_call_children(self, parent_call_seq=None, parent_return_seq=None) -> list[dict]:
        if parent_call_seq is None:
            target_depth = 0
            range_filter = (ExecutionFrames.sequence_no >= 0)
        else:
            parent = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == self.run_id) &
                (ExecutionFrames.sequence_no == parent_call_seq))
            if not parent:
                return []
            target_depth = parent.call_depth + 1
            if parent_return_seq is not None:
                range_filter = (
                    (ExecutionFrames.sequence_no > parent_call_seq) &
                    (ExecutionFrames.sequence_no < parent_return_seq))
            else:
                range_filter = (ExecutionFrames.sequence_no > parent_call_seq)

        events = list(ExecutionFrames.select()
            .where(
                (ExecutionFrames.run_id == self.run_id) &
                (ExecutionFrames.frame_event.in_(['call', 'return', 'exception_unwind'])) &
                (ExecutionFrames.call_depth == target_depth) &
                range_filter)
            .order_by(ExecutionFrames.sequence_no))

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
        frames = ExecutionFrames.select(
            ExecutionFrames.sequence_no,
            ExecutionFrames.line_no,
            ExecutionFrames.filename,
            ExecutionFrames.function_name,
            ExecutionFrames.locals_snapshot,
        ).where(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.frame_event == 'line') &
            (ExecutionFrames.sequence_no >= start_seq) &
            (ExecutionFrames.sequence_no <= end_seq) &
            (ExecutionFrames.locals_snapshot.contains(f'"{variable_name}"'))
        ).order_by(ExecutionFrames.sequence_no).iterator()

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

    # --- Internal helpers ---

    def _evaluate_condition(self, condition: str, seq: int) -> bool:
        if not condition or not condition.strip():
            return True
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == seq))
        if frame is None or not frame.locals_snapshot:
            return True
        try:
            locals_data = json.loads(frame.locals_snapshot)
        except (json.JSONDecodeError, TypeError):
            return True
        eval_locals = {}
        for name, value in locals_data.items():
            eval_locals[name] = _parse_repr_value(value)
        try:
            return bool(eval(condition, {"__builtins__": {}}, eval_locals))
        except Exception:
            return True

    def _find_conditional_hit_forward(self, filename: str, line: int,
                                       condition: str, after_seq: int,
                                       hit_condition: str = '',
                                       log_message: str = '') -> int | None:
        bp_key = (filename, line)

        if not condition or not condition.strip():
            # Fast path: no expression condition
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.filename == filename) &
                          (ExecutionFrames.line_no == line) &
                          (ExecutionFrames.frame_event == 'line') &
                          (ExecutionFrames.sequence_no > after_seq))
                   .order_by(ExecutionFrames.sequence_no)
                   .limit(1).first())
            if hit is None:
                return None
            # Check hit condition
            if hit_condition:
                self._bp_hit_counts[bp_key] = self._bp_hit_counts.get(bp_key, 0) + 1
                if not self._check_hit_condition(hit_condition, self._bp_hit_counts[bp_key]):
                    # Recurse to find next
                    return self._find_conditional_hit_forward(
                        filename, line, condition, hit.sequence_no,
                        hit_condition=hit_condition, log_message=log_message)
            # Handle log points
            if log_message:
                msg = self._format_log_message(log_message, hit.sequence_no)
                self._log_messages.append(msg)
                # Don't stop, but return the seq so caller knows we hit something
                return hit.sequence_no
            return hit.sequence_no

        cursor = after_seq
        for _ in range(10000):
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.filename == filename) &
                          (ExecutionFrames.line_no == line) &
                          (ExecutionFrames.frame_event == 'line') &
                          (ExecutionFrames.sequence_no > cursor))
                   .order_by(ExecutionFrames.sequence_no)
                   .limit(1).first())
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
        return None

    def _find_conditional_hit_reverse(self, filename: str, line: int,
                                       condition: str, before_seq: int) -> int | None:
        if not condition or not condition.strip():
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.filename == filename) &
                          (ExecutionFrames.line_no == line) &
                          (ExecutionFrames.frame_event == 'line') &
                          (ExecutionFrames.sequence_no < before_seq))
                   .order_by(ExecutionFrames.sequence_no.desc())
                   .limit(1).first())
            return hit.sequence_no if hit else None

        cursor = before_seq
        for _ in range(10000):
            hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                   .where((ExecutionFrames.run_id == self.run_id) &
                          (ExecutionFrames.filename == filename) &
                          (ExecutionFrames.line_no == line) &
                          (ExecutionFrames.frame_event == 'line') &
                          (ExecutionFrames.sequence_no < cursor))
                   .order_by(ExecutionFrames.sequence_no.desc())
                   .limit(1).first())
            if hit is None:
                return None
            if self._evaluate_condition(condition, hit.sequence_no):
                return hit.sequence_no
            cursor = hit.sequence_no
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
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == seq))
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
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == seq))
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
        frames = ExecutionFrames.select(
            ExecutionFrames.sequence_no,
            ExecutionFrames.locals_snapshot,
        ).where(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.frame_event == 'line') &
            (ExecutionFrames.sequence_no > after_seq) &
            (ExecutionFrames.locals_snapshot.contains(f'"{var_name}"'))
        ).order_by(ExecutionFrames.sequence_no).limit(10000).iterator()

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
        frames = ExecutionFrames.select(
            ExecutionFrames.sequence_no,
            ExecutionFrames.locals_snapshot,
        ).where(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.frame_event == 'line') &
            (ExecutionFrames.sequence_no < before_seq) &
            (ExecutionFrames.locals_snapshot.contains(f'"{var_name}"'))
        ).order_by(ExecutionFrames.sequence_no.desc()).limit(10000).iterator()

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
        line_bwd = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                    .where((ExecutionFrames.run_id == self.run_id) &
                           (ExecutionFrames.frame_event == 'line') &
                           (ExecutionFrames.sequence_no <= seq))
                    .order_by(ExecutionFrames.sequence_no.desc())
                    .limit(1).first())
        if line_bwd:
            return line_bwd.sequence_no
        line_fwd = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                    .where((ExecutionFrames.run_id == self.run_id) &
                           (ExecutionFrames.frame_event == 'line') &
                           (ExecutionFrames.sequence_no > seq))
                    .order_by(ExecutionFrames.sequence_no)
                    .limit(1).first())
        return line_fwd.sequence_no if line_fwd else seq

    def _get_current_frame(self):
        return ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == self.current_frame_seq))

    def _navigate_to(self, seq: int, reason: str) -> dict:
        old_seq = self.current_frame_seq
        self.current_frame_seq = seq

        # Resolve target thread before updating stack — cross-thread
        # navigation must rebuild from scratch (incremental update would
        # scan the wrong thread's events).
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == seq))
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
            thread_filter = ((ExecutionFrames.thread_id == self.current_thread_id)
                             if self.current_thread_id is not None
                             else (ExecutionFrames.sequence_no >= 0))
            events = list(ExecutionFrames.select()
                          .where((ExecutionFrames.run_id == self.run_id) &
                                 thread_filter &
                                 (ExecutionFrames.sequence_no > old_seq) &
                                 (ExecutionFrames.sequence_no <= new_seq))
                          .order_by(ExecutionFrames.sequence_no))
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
        target = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == seq))
        target_thread = target.thread_id if target else (self.current_thread_id or 0)

        # Find nearest cached stack <= seq for the same thread
        cached_seqs = [s for (s, t) in self._stack_cache if s <= seq and t == target_thread]
        if cached_seqs:
            start_seq = max(cached_seqs)
            stack = [entry.copy() for entry in self._stack_cache[(start_seq, target_thread)]]
            # Cached stacks are DAP order (deepest-first); scan uses call-stack order
            stack.reverse()
            lower_bound = (ExecutionFrames.sequence_no > start_seq)
        else:
            stack = []
            lower_bound = (ExecutionFrames.sequence_no >= 0)

        events = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == self.run_id) &
                             (ExecutionFrames.thread_id == target_thread) &
                             lower_bound &
                             (ExecutionFrames.sequence_no <= seq))
                      .order_by(ExecutionFrames.sequence_no))
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
        return _format_value(value)
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
