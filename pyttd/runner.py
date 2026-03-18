import runpy
import sys
import os

class Runner:
    def run_script(self, script_path: str, cwd: str, args: list[str] | None = None):
        """Execute user script via runpy.run_path."""
        old_cwd = os.getcwd()
        old_argv = sys.argv[:]
        old_path0 = sys.path[0] if sys.path else None
        path_was_empty = not bool(sys.path)
        os.chdir(cwd)
        sys.argv = [script_path] + (args or [])
        script_dir = os.path.dirname(os.path.abspath(script_path))
        if sys.path and sys.path[0] != script_dir:
            sys.path[0] = script_dir
        elif not sys.path:
            sys.path.insert(0, script_dir)
        try:
            runpy.run_path(script_path, run_name='__main__')
        finally:
            sys.argv = old_argv
            os.chdir(old_cwd)
            if path_was_empty:
                if sys.path and sys.path[0] == script_dir:
                    sys.path.pop(0)
            elif old_path0 is not None:
                if sys.path:
                    sys.path[0] = old_path0
                else:
                    sys.path.insert(0, old_path0)

    def run_module(self, module_name: str, cwd: str, args: list[str] | None = None):
        """Execute user module via runpy.run_module."""
        old_cwd = os.getcwd()
        old_argv = sys.argv[:]
        os.chdir(cwd)
        sys.argv = [module_name] + (args or [])
        try:
            runpy.run_module(module_name, run_name='__main__', alter_sys=True)
        finally:
            sys.argv = old_argv
            os.chdir(old_cwd)
