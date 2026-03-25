"""Tests for slots-based dataclass support (Item 3)."""
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
    """Find the last line frame where the given variable appears in locals."""
    return db.fetchone(
        "SELECT * FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line' AND locals_snapshot LIKE ?"
        " ORDER BY sequence_no DESC LIMIT 1",
        (str(run_id), f'%"{var_name}"%'))


class TestSlotsSupport:
    def test_slots_dataclass_expandable(self, record_func):
        """Record a @dataclass(slots=True) and verify children are captured."""
        db_path, run_id, stats = record_func('''
            from dataclasses import dataclass

            @dataclass(slots=True)
            class Point:
                x: float
                y: float

            p = Point(1.0, 2.0)
            _ = p
        ''')
        frame = _find_frame_with_var(run_id, 'p')
        assert frame is not None, "Should record a frame with 'p' in locals"
        data = json.loads(frame.locals_snapshot)
        assert 'p' in data, "Variable 'p' should be in locals"
        val = data['p']
        assert isinstance(val, dict), "Point should be serialized as structured dict"
        assert val.get('__type__') == 'object', "Type should be 'object'"
        children = val.get('__children__', [])
        keys = [c['key'] for c in children]
        assert 'x' in keys, f"Expected 'x' in slot children, got {keys}"
        assert 'y' in keys, f"Expected 'y' in slot children, got {keys}"
        # Verify values
        x_child = next(c for c in children if c['key'] == 'x')
        assert x_child['value'] == '1.0'
        y_child = next(c for c in children if c['key'] == 'y')
        assert y_child['value'] == '2.0'

    def test_manual_slots_expandable(self, record_func):
        """Record an object with manual __slots__ and verify children."""
        db_path, run_id, stats = record_func('''
            class ManualSlots:
                __slots__ = ('name', 'value')
                def __init__(self, name, value):
                    self.name = name
                    self.value = value

            obj = ManualSlots("test", 42)
            _ = obj
        ''')
        frame = _find_frame_with_var(run_id, 'obj')
        assert frame is not None, "Should record a frame with 'obj' in locals"
        data = json.loads(frame.locals_snapshot)
        assert 'obj' in data, "Variable 'obj' should be in locals"
        val = data['obj']
        assert isinstance(val, dict), "ManualSlots should be serialized as structured dict"
        assert val.get('__type__') == 'object'
        children = val.get('__children__', [])
        keys = [c['key'] for c in children]
        assert 'name' in keys, f"Expected 'name' in slot children, got {keys}"
        assert 'value' in keys, f"Expected 'value' in slot children, got {keys}"
        name_child = next(c for c in children if c['key'] == 'name')
        assert name_child['value'] == "'test'"
        value_child = next(c for c in children if c['key'] == 'value')
        assert value_child['value'] == '42'

    def test_slots_via_session_get_variables(self, record_func):
        """Slots children are accessible via Session.get_variables_at."""
        db_path, run_id, stats = record_func('''
            class Vec2:
                __slots__ = ('x', 'y')
                def __init__(self, x, y):
                    self.x = x
                    self.y = y

            v = Vec2(3, 4)
            _ = v
        ''')
        session = _setup_session(run_id)
        frame = _find_frame_with_var(run_id, 'v')
        assert frame is not None
        variables = session.get_variables_at(frame.sequence_no)
        v_var = next((var for var in variables if var['name'] == 'v'), None)
        assert v_var is not None, "Variable 'v' should appear in session variables"
        assert v_var['variablesReference'] > 0, "Slots object should be expandable"
        children = session.get_variable_children(v_var['variablesReference'])
        child_names = {c['name'] for c in children}
        assert 'x' in child_names
        assert 'y' in child_names

    def test_namedtuple_field_names(self, record_func):
        """NamedTuple children use field names instead of numeric indices."""
        db_path, run_id, stats = record_func('''
            from typing import NamedTuple

            class Coords(NamedTuple):
                lat: float
                lon: float

            c = Coords(37.7, -122.4)
            _ = c
        ''')
        frame = _find_frame_with_var(run_id, 'c')
        assert frame is not None, "Should record a frame with 'c' in locals"
        data = json.loads(frame.locals_snapshot)
        assert 'c' in data
        val = data['c']
        assert isinstance(val, dict)
        children = val.get('__children__', [])
        assert len(children) == 2, f"Coords has 2 fields, got {len(children)}"
        keys = [ch['key'] for ch in children]
        assert 'lat' in keys, f"Expected field name 'lat', got keys {keys}"
        assert 'lon' in keys, f"Expected field name 'lon', got keys {keys}"

    def test_namedtuple_collections(self, record_func):
        """collections.namedtuple also uses field names."""
        db_path, run_id, stats = record_func('''
            from collections import namedtuple

            Color = namedtuple("Color", ["red", "green", "blue"])
            col = Color(255, 128, 0)
            _ = col
        ''')
        frame = _find_frame_with_var(run_id, 'col')
        assert frame is not None
        data = json.loads(frame.locals_snapshot)
        assert 'col' in data
        val = data['col']
        assert isinstance(val, dict)
        children = val.get('__children__', [])
        assert len(children) == 3
        keys = [ch['key'] for ch in children]
        assert 'red' in keys
        assert 'green' in keys
        assert 'blue' in keys

    def test_slots_with_unset_slot(self, record_func):
        """Unset slots are gracefully skipped; recording does not crash."""
        db_path, run_id, stats = record_func('''
            class Partial:
                __slots__ = ('a', 'b')
                def __init__(self):
                    self.a = 1
                    # b is intentionally not set

            obj = Partial()
            _ = obj
        ''')
        assert stats['frame_count'] > 0, "Recording should complete without crashing"
        frame = _find_frame_with_var(run_id, 'obj')
        assert frame is not None
        data = json.loads(frame.locals_snapshot)
        assert 'obj' in data
        val = data['obj']
        # Only 'a' should appear; 'b' is unset and skipped
        if isinstance(val, dict) and '__children__' in val:
            children = val['__children__']
            keys = [c['key'] for c in children]
            assert 'a' in keys
            assert 'b' not in keys, "Unset slot 'b' should not appear in children"

    def test_regular_tuple_still_uses_indices(self, record_func):
        """Regular (non-named) tuples still use numeric indices as keys."""
        db_path, run_id, stats = record_func('''
            t = (10, 20, 30)
            _ = t
        ''')
        frame = _find_frame_with_var(run_id, 't')
        assert frame is not None
        data = json.loads(frame.locals_snapshot)
        assert 't' in data
        val = data['t']
        assert isinstance(val, dict)
        children = val.get('__children__', [])
        assert len(children) == 3
        keys = [ch['key'] for ch in children]
        assert '0' in keys
        assert '1' in keys
        assert '2' in keys
