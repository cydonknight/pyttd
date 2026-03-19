"""Tests for secrets filtering (Phase 8B)."""
import json

from pyttd.config import PyttdConfig
from pyttd.models.frames import ExecutionFrames
from pyttd.session import Session


class TestSecretsFiltering:
    def test_secret_variable_redacted(self, record_func):
        db_path, run_id, _ = record_func('''
            password = "hunter2"
            x = 42
            _ = x
        ''')
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.is_null(False)))
                 .order_by(ExecutionFrames.sequence_no.desc())
                 .first())
        assert frame is not None
        locals_data = json.loads(frame.locals_snapshot)
        assert 'password' in locals_data
        assert locals_data['password'] == '<redacted>'
        assert 'x' in locals_data
        assert locals_data['x'] != '<redacted>'

    def test_secret_pattern_case_insensitive(self, record_func):
        db_path, run_id, _ = record_func('''
            API_TOKEN = "abc123"
            count = 10
            _ = count
        ''')
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.is_null(False)))
                 .order_by(ExecutionFrames.sequence_no.desc())
                 .first())
        assert frame is not None
        locals_data = json.loads(frame.locals_snapshot)
        assert locals_data.get('API_TOKEN') == '<redacted>'
        assert locals_data.get('count') != '<redacted>'

    def test_secret_pattern_substring(self, record_func):
        db_path, run_id, _ = record_func('''
            authorization_header = "Bearer xyz"
            name = "test"
            _ = name
        ''')
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.is_null(False)))
                 .order_by(ExecutionFrames.sequence_no.desc())
                 .first())
        assert frame is not None
        locals_data = json.loads(frame.locals_snapshot)
        assert locals_data.get('authorization_header') == '<redacted>'
        assert locals_data.get('name') != '<redacted>'

    def test_no_redact_mode(self, tmp_path):
        """redact_secrets=False preserves all variable values."""
        import textwrap, runpy, sys
        from pyttd.recorder import Recorder
        from pyttd.models.storage import delete_db_files, close_db
        from pyttd.models.base import db

        script_file = tmp_path / "test_script.py"
        script_file.write_text(textwrap.dedent('''
            password = "hunter2"
            x = 42
            _ = x
        '''))
        db_path = str(tmp_path / "test.pyttd.db")
        delete_db_files(db_path)

        config = PyttdConfig(checkpoint_interval=0, redact_secrets=False)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        recorder.stop()
        run_id = recorder.run_id

        try:
            frame = (ExecutionFrames.select()
                     .where((ExecutionFrames.run_id == run_id) &
                            (ExecutionFrames.frame_event == 'line') &
                            (ExecutionFrames.locals_snapshot.is_null(False)))
                     .order_by(ExecutionFrames.sequence_no.desc())
                     .first())
            assert frame is not None
            locals_data = json.loads(frame.locals_snapshot)
            assert locals_data.get('password') != '<redacted>'
        finally:
            close_db()
            db.init(None)

    def test_custom_secret_patterns(self, tmp_path):
        """Custom patterns work for redaction."""
        import textwrap, runpy, sys
        from pyttd.recorder import Recorder
        from pyttd.models.storage import delete_db_files, close_db
        from pyttd.models.base import db

        script_file = tmp_path / "test_script.py"
        script_file.write_text(textwrap.dedent('''
            my_custom_field = "sensitive"
            normal = 42
            _ = normal
        '''))
        db_path = str(tmp_path / "test.pyttd.db")
        delete_db_files(db_path)

        config = PyttdConfig(checkpoint_interval=0,
                             secret_patterns=['my_custom'])
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        recorder.stop()
        run_id = recorder.run_id

        try:
            frame = (ExecutionFrames.select()
                     .where((ExecutionFrames.run_id == run_id) &
                            (ExecutionFrames.frame_event == 'line') &
                            (ExecutionFrames.locals_snapshot.is_null(False)))
                     .order_by(ExecutionFrames.sequence_no.desc())
                     .first())
            assert frame is not None
            locals_data = json.loads(frame.locals_snapshot)
            assert locals_data.get('my_custom_field') == '<redacted>'
            assert locals_data.get('normal') != '<redacted>'
        finally:
            close_db()
            db.init(None)

    def test_redacted_variables_still_appear_in_session(self, record_func):
        db_path, run_id, _ = record_func('''
            password = "hunter2"
            x = 42
            _ = x
        ''')
        session = Session()
        first_line = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no)
                      .first())
        session.enter_replay(run_id, first_line.sequence_no)
        # Find a frame with both vars
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"password"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None, "Expected frame with 'password' in locals"
        variables = session.get_variables_at(frame.sequence_no)
        names = {v['name'] for v in variables}
        assert 'password' in names
        pw_var = next(v for v in variables if v['name'] == 'password')
        assert pw_var['value'] == '<redacted>'

    def test_set_secret_patterns_clears_previous(self, tmp_path):
        """Second call to set_secret_patterns replaces, doesn't append."""
        import textwrap, runpy, sys
        from pyttd.recorder import Recorder
        from pyttd.models.storage import delete_db_files, close_db
        from pyttd.models.base import db

        script_file = tmp_path / "test_script.py"
        script_file.write_text(textwrap.dedent('''
            old_pattern_var = "visible"
            new_pattern_var = "hidden"
            _ = old_pattern_var
        '''))
        db_path = str(tmp_path / "test.pyttd.db")
        delete_db_files(db_path)

        # Use only ['new_pattern'] — old_pattern should NOT be redacted
        config = PyttdConfig(checkpoint_interval=0,
                             secret_patterns=['new_pattern'])
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        recorder.stop()
        run_id = recorder.run_id

        try:
            frame = (ExecutionFrames.select()
                     .where((ExecutionFrames.run_id == run_id) &
                            (ExecutionFrames.frame_event == 'line') &
                            (ExecutionFrames.locals_snapshot.is_null(False)))
                     .order_by(ExecutionFrames.sequence_no.desc())
                     .first())
            assert frame is not None
            locals_data = json.loads(frame.locals_snapshot)
            # old_pattern_var should NOT be redacted (not in patterns)
            assert locals_data.get('old_pattern_var') != '<redacted>'
            # new_pattern_var SHOULD be redacted
            assert locals_data.get('new_pattern_var') == '<redacted>'
        finally:
            close_db()
            db.init(None)
