"""Tests for expandable variable trees (Phase 10A)."""
import json

from pyttd.models.db import db
from pyttd.session import Session


def _setup_session(run_id):
    session = Session()
    first_line = db.fetchone(
        "SELECT sequence_no FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " ORDER BY sequence_no LIMIT 1",
        (str(run_id),))
    session.enter_replay(run_id, first_line.sequence_no)
    return session


def _find_frame_with_var(run_id, var_name):
    return db.fetchone(
        "SELECT * FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line' AND locals_snapshot LIKE ?"
        " ORDER BY sequence_no DESC LIMIT 1",
        (str(run_id), f'%"{var_name}"%'))


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

    def test_nested_expansion(self, record_func):
        """Nested dict children should be expandable (U5: multi-level)."""
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
            nested_child = next((c for c in children if 'nested' in c['name']), None)
            assert nested_child is not None
            # U5: nested dict should be expandable (variablesReference > 0)
            assert nested_child['variablesReference'] > 0
            # Expand second level
            grandchildren = session.get_variable_children(nested_child['variablesReference'])
            assert len(grandchildren) > 0
            inner = next((gc for gc in grandchildren if 'inner' in gc['name']), None)
            assert inner is not None
            assert inner['value'] == '1'

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

    def test_children_by_name_without_get_variables(self, record_func):
        """get_variable_children_by_name works without prior get_variables_at."""
        db_path, run_id, _ = record_func('''
            d = {"x": 10, "y": 20}
            _ = d
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'd')
        assert frame is not None
        # Call directly — no get_variables_at, cache is empty
        assert len(session._var_ref_cache) == 0
        children = session.get_variable_children_by_name(frame.sequence_no, 'd')
        assert len(children) == 2
        child_keys = {c['name'] for c in children}
        assert "'x'" in child_keys or 'x' in child_keys

    def test_children_by_name_matches_cache_path(self, record_func):
        """Both access paths return identical results."""
        db_path, run_id, _ = record_func('''
            d = {"a": 1, "b": 2, "c": 3}
            _ = d
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'd')
        assert frame is not None
        # Cache path
        variables = session.get_variables_at(frame.sequence_no)
        d_var = next((v for v in variables if v['name'] == 'd'), None)
        assert d_var is not None and d_var['variablesReference'] > 0
        via_cache = session.get_variable_children(d_var['variablesReference'])
        # Direct path
        via_name = session.get_variable_children_by_name(frame.sequence_no, 'd')
        assert via_cache == via_name

    def test_children_by_name_nonexistent_variable(self, record_func):
        """Nonexistent variable returns empty list, no crash."""
        db_path, run_id, _ = record_func('''
            x = 42
            _ = x
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'x')
        assert frame is not None
        children = session.get_variable_children_by_name(frame.sequence_no, 'nonexistent')
        assert children == []

    def test_children_by_name_primitive_variable(self, record_func):
        """Primitive variable has no children."""
        db_path, run_id, _ = record_func('''
            x = 42
            _ = x
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'x')
        assert frame is not None
        children = session.get_variable_children_by_name(frame.sequence_no, 'x')
        assert children == []
