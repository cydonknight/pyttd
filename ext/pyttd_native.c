#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "platform.h"
#include "frame_event.h"

#include "recorder.h"
#include "ringbuf.h"
#include "checkpoint.h"
#include "checkpoint_store.h"
#include "replay.h"
#include "iohook.h"

static PyMethodDef PyttdMethods[] = {
    {"start_recording", (PyCFunction)pyttd_start_recording, METH_VARARGS | METH_KEYWORDS,
     "Start recording with frame eval hook. Args: flush_callback, buffer_size, flush_interval_ms, "
     "checkpoint_callback, checkpoint_interval, io_flush_callback, io_replay_loader"},
    {"stop_recording", (PyCFunction)pyttd_stop_recording, METH_NOARGS,
     "Stop recording and flush ring buffer"},
    {"get_recording_stats", (PyCFunction)pyttd_get_recording_stats, METH_NOARGS,
     "Return dict with frame_count, elapsed_time, etc."},
    {"set_ignore_patterns", (PyCFunction)pyttd_set_ignore_patterns, METH_VARARGS,
     "Set filename/function patterns to ignore during recording"},
    {"request_stop", (PyCFunction)pyttd_request_stop, METH_NOARGS,
     "Set atomic stop flag checked by frame eval hook (for interrupt)"},
    {"set_recording_thread", (PyCFunction)pyttd_set_recording_thread, METH_NOARGS,
     "Set current thread as the recording thread (for server mode)"},
    {"create_checkpoint", (PyCFunction)pyttd_create_checkpoint, METH_NOARGS,
     "Fork a checkpoint (Unix only)"},
    {"restore_checkpoint", (PyCFunction)pyttd_restore_checkpoint, METH_VARARGS,
     "Find nearest checkpoint <= target_seq, resume child, fast-forward, return state dict"},
    {"kill_all_checkpoints", (PyCFunction)pyttd_kill_all_checkpoints, METH_NOARGS,
     "Send DIE to all checkpoint children"},
    {"get_checkpoint_count", (PyCFunction)pyttd_get_checkpoint_count, METH_NOARGS,
     "Return number of live checkpoint children"},
    {"set_secret_patterns", (PyCFunction)pyttd_set_secret_patterns, METH_VARARGS,
     "Set variable name patterns for secret redaction during recording"},
    {"set_include_patterns", (PyCFunction)pyttd_set_include_patterns, METH_VARARGS,
     "Set function name patterns for selective recording (empty = record all)"},
    {"set_max_frames", (PyCFunction)pyttd_set_max_frames, METH_VARARGS,
     "Set maximum frame count for auto-stop (0 = unlimited)"},
    {"set_file_include_patterns", (PyCFunction)pyttd_set_file_include_patterns, METH_VARARGS,
     "Set file path glob patterns for selective recording (empty = record all)"},
    {"set_exclude_patterns", (PyCFunction)pyttd_set_exclude_patterns, METH_VARARGS,
     "Set exclude patterns: (func_patterns, file_patterns). Excluded functions/files are never recorded."},
    {"trace_current_frame", pyttd_trace_current_frame, METH_NOARGS,
     "Install trace function on current thread to capture line events in the caller's frame."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef pyttd_module = {
    PyModuleDef_HEAD_INIT,
    "pyttd_native",
    "Python Time-Travel Debugger native extension",
    -1,
    PyttdMethods
};

PyMODINIT_FUNC PyInit_pyttd_native(void) {
    #if PY_VERSION_HEX < 0x030C0000
    PyErr_SetString(PyExc_ImportError, "pyttd requires Python 3.12 or later");
    return NULL;
    #endif
    return PyModule_Create(&pyttd_module);
}
