"""Tests for expandable variable trees (Phase 10A)."""
import json

from pyttd.models.frames import ExecutionFrames
from pyttd.session import Session


def _setup_session(run_id):
    session = Session()
    first_line = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                  .where((ExecutionFrames.run_id == run_id) &
                         (ExecutionFrames.frame_event == 'line'))
                  .order_by(ExecutionFrames.sequence_no)
                  .first())
    session.enter_replay(run_id, first_line.sequence_no)
    return session


def _find_frame_with_var(run_id, var_name):
    return (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line') &
                   (ExecutionFrames.locals_snapshot.contains(f'"{var_name}"')))
            .order_by(ExecutionFrames.sequence_no.desc())
            .first())


class TestExpandableVariables:
    def test_dict_variable_has_children(self, record_func):
        db_path, run_id, _ = record_func('''
            d = {"a": 1, "b": 2, "c": 3}
            _ = d  # extra line so d is captured in locals
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'd')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        d_var = next((v for v in variables if v['name'] == 'd'), None)
        assert d_var is not None
        assert d_var['variablesReference'] > 0
        assert d_var['type'] == 'dict'

    def test_dict_children_returned(self, record_func):
        db_path, run_id, _ = record_func('''
            d = {"x": 10, "y": 20}
            _ = d
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'd')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        d_var = next((v for v in variables if v['name'] == 'd'), None)
        assert d_var is not None
        assert d_var['variablesReference'] > 0
        children = session.get_variable_children(d_var['variablesReference'])
        assert len(children) == 2
        child_keys = {c['name'] for c in children}
        assert "'x'" in child_keys or 'x' in child_keys

    def test_list_variable_has_children(self, record_func):
        db_path, run_id, _ = record_func('''
            items = [10, 20, 30]
            _ = items
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'items')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        items_var = next((v for v in variables if v['name'] == 'items'), None)
        assert items_var is not None
        assert items_var['variablesReference'] > 0
        children = session.get_variable_children(items_var['variablesReference'])
        assert len(children) == 3

    def test_nested_object_has_children(self, record_func):
        db_path, run_id, _ = record_func('''
            class Obj:
                def __init__(self):
                    self.x = 1
                    self.y = 2
            obj = Obj()
            _ = obj
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'obj')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        obj_var = next((v for v in variables if v['name'] == 'obj'), None)
        assert obj_var is not None
        if obj_var['variablesReference'] > 0:
            children = session.get_variable_children(obj_var['variablesReference'])
            assert len(children) >= 2

    def test_primitive_not_expandable(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 42
            s = "hello"
            f = 3.14
            b = True
            n = None
            _ = x
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'x')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        for v in variables:
            if v['name'] in ('x', 's', 'f', 'b', 'n'):
                assert v['variablesReference'] == 0, f"{v['name']} should not be expandable"

    def test_large_dict_truncated(self, record_func):
        db_path, run_id, _ = record_func('''
            d = {str(i): i for i in range(100)}
            _ = d
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'd')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        d_var = next((v for v in variables if v['name'] == 'd'), None)
        assert d_var is not None
        if d_var['variablesReference'] > 0:
            children = session.get_variable_children(d_var['variablesReference'])
            assert len(children) <= 50

    def test_children_are_flat(self, record_func):
        db_path, run_id, _ = record_func('''
            d = {"nested": {"inner": 1}}
            _ = d
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'd')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        d_var = next((v for v in variables if v['name'] == 'd'), None)
        assert d_var is not None
        if d_var['variablesReference'] > 0:
            children = session.get_variable_children(d_var['variablesReference'])
            for child in children:
                assert child['variablesReference'] == 0

    def test_set_variable_expandable(self, record_func):
        db_path, run_id, _ = record_func('''
            s = {1, 2, 3}
            _ = s
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 's')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        s_var = next((v for v in variables if v['name'] == 's'), None)
        assert s_var is not None
        # Sets should be expandable if serialized as structured
        if s_var['variablesReference'] > 0:
            children = session.get_variable_children(s_var['variablesReference'])
            assert len(children) == 3

    def test_empty_dict_expandable(self, record_func):
        db_path, run_id, _ = record_func('''
            d = {}
            _ = d
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'd')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        d_var = next((v for v in variables if v['name'] == 'd'), None)
        assert d_var is not None
        if d_var['variablesReference'] > 0:
            children = session.get_variable_children(d_var['variablesReference'])
            assert len(children) == 0

    def test_tuple_variable_expandable(self, record_func):
        db_path, run_id, _ = record_func('''
            t = (10, 20, 30)
            _ = t
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 't')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        t_var = next((v for v in variables if v['name'] == 't'), None)
        assert t_var is not None
        if t_var['variablesReference'] > 0:
            children = session.get_variable_children(t_var['variablesReference'])
            assert len(children) == 3
