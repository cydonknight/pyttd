__version__ = "0.7.0"

from pyttd.main import ttdbg, start_recording, stop_recording, arm, disarm, install_signal_handler

__all__ = ["__version__", "ttdbg", "start_recording", "stop_recording",
           "arm", "disarm", "install_signal_handler"]

import os as _os
if _os.environ.get('PYTTD_ARM_SIGNAL'):
    import signal as _sig
    _signum = getattr(_sig, 'SIG' + _os.environ['PYTTD_ARM_SIGNAL'].upper(), None)
    if _signum:
        install_signal_handler(_signum)
