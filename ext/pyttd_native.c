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
#include "binlog.h"

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
    {"set_exclude_locals_patterns", (PyCFunction)pyttd_set_exclude_locals_patterns, METH_VARARGS,
     "Set file globs for which events are recorded but locals are suppressed. Empty = no exclusions."},
    {"set_locals_max_depth", (PyCFunction)pyttd_set_locals_max_depth, METH_VARARGS,
     "Set max call depth beyond which locals are skipped (0 disables). Default 20."},
    {"trace_current_frame", pyttd_trace_current_frame, METH_NOARGS,
     "Install trace function on current thread to capture line events in the caller's frame."},
    {"get_checkpoint_memory", (PyCFunction)pyttd_get_checkpoint_memory, METH_NOARGS,
     "Return dict with checkpoint memory info (total_bytes, entries, etc.)"},
    {"set_checkpoint_memory_limit", (PyCFunction)pyttd_set_checkpoint_memory_limit, METH_VARARGS,
     "Set checkpoint memory limit in bytes (0 = unlimited). Triggers aggressive eviction."},
    {"binlog_open", (PyCFunction)pyttd_binlog_open, METH_VARARGS,
     "Open binary log for recording"},
    {"binlog_load", (PyCFunction)pyttd_binlog_load, METH_VARARGS,
     "Load binary log into SQLite database"},
    {"binlog_set_size_limit", (PyCFunction)pyttd_binlog_set_size_limit, METH_VARARGS,
     "Set binlog file size limit in bytes for auto-stop during recording"},
    {"binlog_flush", (PyCFunction)pyttd_binlog_flush, METH_NOARGS,
     "Flush binlog stdio buffer to disk"},
    {"binlog_load_partial", (PyCFunction)pyttd_binlog_load_partial, METH_VARARGS,
     "Incrementally load binlog records into SQLite (for pause snapshot)"},
    {"request_pause", (PyCFunction)pyttd_request_user_pause, METH_NOARGS,
     "Request recording thread to pause at next LINE event. Returns True on success."},
    {"resume", (PyCFunction)pyttd_user_resume, METH_NOARGS,
     "Resume recording thread after pause"},
    {"is_paused", (PyCFunction)pyttd_is_user_paused, METH_NOARGS,
     "Return True if recording thread is currently paused"},
    {"get_sequence_counter", (PyCFunction)pyttd_get_sequence_counter, METH_NOARGS,
     "Return current global sequence counter value"},
    {"flush_and_wait", (PyCFunction)pyttd_flush_and_wait, METH_NOARGS,
     "Trigger immediate flush thread cycle and wait for completion"},
    {"resume_live", (PyCFunction)pyttd_resume_live, METH_VARARGS,
     "Resume live execution from a checkpoint child (sends RESUME_LIVE, child takes over)"},
    {"set_socket_fd", (PyCFunction)pyttd_set_socket_fd, METH_VARARGS,
     "Set the TCP socket FD for checkpoint child handoff"},
    {"set_variable", (PyCFunction)pyttd_set_variable, METH_VARARGS,
     "Modify a variable in the paused frame. Args: (var_name, new_value_expr). Only works when paused."},
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
