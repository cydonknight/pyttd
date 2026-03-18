#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <frameobject.h>
#include <stdatomic.h>
#include <string.h>
#include <time.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <pthread.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#endif

#include "platform.h"
#include "frame_event.h"
#include "recorder.h"
#include "ringbuf.h"
#include "iohook.h"

#ifdef PYTTD_HAS_FORK
#include "checkpoint.h"
#include "checkpoint_store.h"
#endif

/* ---- Version-gated macros for PEP 523 eval hook API ---- */

#if PY_VERSION_HEX >= 0x030F0000    /* 3.15+ */
  #define PYTTD_SET_EVAL_FUNC PyUnstable_InterpreterState_SetEvalFrameFunc
  #define PYTTD_GET_EVAL_FUNC PyUnstable_InterpreterState_GetEvalFrameFunc
#else                                 /* 3.12 - 3.14 */
  #define PYTTD_SET_EVAL_FUNC _PyInterpreterState_SetEvalFrameFunc
  #define PYTTD_GET_EVAL_FUNC _PyInterpreterState_GetEvalFrameFunc
#endif

/* Eval hook signature */
typedef PyObject *(*EvalFrameFunc)(PyThreadState *, struct _PyInterpreterFrame *, int);

/* ---- Maximum sizes ---- */
#define MAX_IGNORE_PATTERNS    64
#define MAX_REPR_LENGTH        256
#define FLUSH_BATCH_SIZE       4096

/* ---- Global State ---- */

_Atomic int g_recording = 0;
_Atomic int g_stop_requested = 0;
static _Atomic int g_interpreter_alive = 1;
_Atomic uint64_t g_sequence_counter = 0;
PYTTD_THREAD_LOCAL int g_call_depth = -1;
unsigned long g_main_thread_id = 0;
static EvalFrameFunc g_original_eval = NULL;
static PyObject *g_flush_callback = NULL;
static double g_start_time = 0.0;
static double g_stop_time = 0.0;
PYTTD_THREAD_LOCAL int g_inside_repr = 0;

/* ---- Phase 2: Checkpoint/fast-forward state ---- */
static int g_fast_forward = 0;
static uint64_t g_fast_forward_target = 0;
_Atomic uint64_t g_last_checkpoint_seq = 0;
static PyObject *g_checkpoint_callback = NULL;
static int g_checkpoint_interval = 0;
static PYTTD_THREAD_LOCAL int g_in_checkpoint = 0;  /* guard against recursive checkpoint triggering */

/* Child-only globals (set during child init) */
int g_cmd_fd = -1;
int g_result_fd = -1;
PyThreadState *g_saved_tstate = NULL;

/* ---- Stats ---- */
_Atomic uint64_t g_frame_count = 0;
static _Atomic uint64_t g_flush_count = 0;

/* ---- Ignore Filter ---- */
typedef struct {
    char *patterns[MAX_IGNORE_PATTERNS];
    int count;
} SubstringFilter;

typedef struct {
    char *entries[MAX_IGNORE_PATTERNS * 2];
    int count;
} ExactMatchSet;

static SubstringFilter g_dir_filter = {.count = 0};
static ExactMatchSet g_exact_filter = {.count = 0};

/* ---- Flush Thread ---- */
#ifdef _WIN32
static HANDLE g_flush_thread = NULL;
static HANDLE g_flush_mutex_win = NULL;
static HANDLE g_flush_event = NULL;
#else
static pthread_t g_flush_thread;
int g_flush_thread_created = 0;
pthread_mutex_t g_flush_mutex = PTHREAD_MUTEX_INITIALIZER;
pthread_cond_t g_flush_cond = PTHREAD_COND_INITIALIZER;

/* Phase 2: Pre-fork synchronization condvars (non-static: accessed by checkpoint.c) */
pthread_cond_t g_pause_ack_cv = PTHREAD_COND_INITIALIZER;
pthread_cond_t g_resume_cv = PTHREAD_COND_INITIALIZER;
_Atomic int g_pause_requested = 0;
_Atomic int g_pause_acked = 0;
#endif
static _Atomic int g_flush_stop = 0;
static int g_flush_interval_ms = 10;

/* ---- Phase 2 getter/setter ---- */

void recorder_set_fast_forward(int enabled, uint64_t target_seq) {
    g_fast_forward = enabled;
    g_fast_forward_target = target_seq;
}

uint64_t recorder_get_sequence_counter(void) {
    return atomic_load_explicit(&g_sequence_counter, memory_order_relaxed);
}

int recorder_get_call_depth(void) {
    return g_call_depth;
}

/* ---- Monotonic Clock ---- */

static double get_monotonic_time(void) {
#ifdef _WIN32
    static LARGE_INTEGER freq = {0};
    LARGE_INTEGER counter;
    if (freq.QuadPart == 0) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart / (double)freq.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
#endif
}

/* ---- Ignore Filter Implementation ---- */

static void clear_filters(void) {
    for (int i = 0; i < g_dir_filter.count; i++) {
        free(g_dir_filter.patterns[i]);
        g_dir_filter.patterns[i] = NULL;
    }
    g_dir_filter.count = 0;

    for (int i = 0; i < g_exact_filter.count; i++) {
        free(g_exact_filter.entries[i]);
        g_exact_filter.entries[i] = NULL;
    }
    g_exact_filter.count = 0;
}

static int should_ignore(const char *filename, const char *funcname) {
    /* Fast check: CPython frozen modules (<frozen runpy>, <frozen importlib._bootstrap>,
     * etc.) are never user code. One prefix check catches them all. */
    if (strncmp(filename, "<frozen ", 8) == 0) {
        return 1;
    }

    /* Check substring patterns (directory patterns containing '/') */
    for (int i = 0; i < g_dir_filter.count; i++) {
        if (strstr(filename, g_dir_filter.patterns[i]) != NULL) {
            return 1;
        }
    }

    /* Check exact match patterns against basename of filename and function name */
    const char *basename = strrchr(filename, '/');
    if (!basename) basename = strrchr(filename, '\\');
    if (basename) basename++; else basename = filename;

    for (int i = 0; i < g_exact_filter.count; i++) {
        if (strcmp(basename, g_exact_filter.entries[i]) == 0 ||
            strcmp(funcname, g_exact_filter.entries[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

/* ---- JSON Escaping ---- */

int recorder_json_escape_string(const char *src, char *dst, size_t dst_size) {
    if (!src) {
        if (dst_size < 5) return -1;
        memcpy(dst, "null", 5);
        return 4;
    }
    size_t pos = 0;
    for (const char *p = src; *p; p++) {
        unsigned char c = (unsigned char)*p;
        if (c == '"') {
            if (pos + 2 > dst_size - 1) return -1;
            dst[pos++] = '\\'; dst[pos++] = '"';
        } else if (c == '\\') {
            if (pos + 2 > dst_size - 1) return -1;
            dst[pos++] = '\\'; dst[pos++] = '\\';
        } else if (c == '\n') {
            if (pos + 2 > dst_size - 1) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'n';
        } else if (c == '\r') {
            if (pos + 2 > dst_size - 1) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'r';
        } else if (c == '\t') {
            if (pos + 2 > dst_size - 1) return -1;
            dst[pos++] = '\\'; dst[pos++] = 't';
        } else if (c == '\b') {
            if (pos + 2 > dst_size - 1) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'b';
        } else if (c == '\f') {
            if (pos + 2 > dst_size - 1) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'f';
        } else if (c < 0x20) {
            if (pos + 6 > dst_size - 1) return -1;
            pos += snprintf(dst + pos, dst_size - pos, "\\u%04x", c);
        } else {
            if (pos + 1 > dst_size - 1) return -1;
            dst[pos++] = (char)c;
        }
    }
    dst[pos] = '\0';
    return (int)pos;
}

/* Keep internal alias for backward compat within this file */
static int json_escape_string(const char *src, char *dst, size_t dst_size) {
    return recorder_json_escape_string(src, dst, dst_size);
}

/* ---- Locals Serialization ---- */

static int serialize_one_local(const char *key_str, PyObject *value,
                               char *buf, size_t buf_size,
                               size_t *pos, int *first, size_t *last_complete_pos) {
    g_inside_repr = 1;
    PyObject *repr = PyObject_Repr(value);
    g_inside_repr = 0;
    if (!repr) { PyErr_Clear(); return 1; }
    const char *repr_str = PyUnicode_AsUTF8(repr);
    if (!repr_str) { Py_DECREF(repr); PyErr_Clear(); return 1; }

    /* Truncate repr if too long */
    char repr_truncated[MAX_REPR_LENGTH + 4];
    size_t repr_len = strlen(repr_str);
    if (repr_len > MAX_REPR_LENGTH) {
        memcpy(repr_truncated, repr_str, MAX_REPR_LENGTH);
        memcpy(repr_truncated + MAX_REPR_LENGTH, "...", 4);
        repr_str = repr_truncated;
    }

    if (!*first) {
        if (*pos + 2 >= buf_size) { Py_DECREF(repr); return 0; }
        buf[(*pos)++] = ',';
        buf[(*pos)++] = ' ';
    }
    *first = 0;

    /* Write "key": "escaped_repr" */
    if (*pos + 12 >= buf_size) { Py_DECREF(repr); return 0; }
    buf[(*pos)++] = '"';
    int esc_len = json_escape_string(key_str, buf + *pos, buf_size - *pos - 10);
    if (esc_len < 0) { Py_DECREF(repr); return 0; }
    *pos += esc_len;
    if (*pos + 5 >= buf_size) { Py_DECREF(repr); return 0; }
    buf[(*pos)++] = '"';
    buf[(*pos)++] = ':';
    buf[(*pos)++] = ' ';
    buf[(*pos)++] = '"';
    if (*pos + 4 >= buf_size) { Py_DECREF(repr); return 0; }
    esc_len = json_escape_string(repr_str, buf + *pos, buf_size - *pos - 3);
    if (esc_len < 0) { Py_DECREF(repr); return 0; }
    *pos += esc_len;
    if (*pos + 2 >= buf_size) { Py_DECREF(repr); return 0; }
    buf[(*pos)++] = '"';
    *last_complete_pos = *pos;
    Py_DECREF(repr);
    return 1;
}

const char *recorder_serialize_locals(PyObject *frame_obj, char *buf, size_t buf_size,
                                       PyObject *extra_key, PyObject *extra_val) {
    PyObject *locals = PyFrame_GetLocals((PyFrameObject *)frame_obj);
    if (!locals) {
        PyErr_Clear();
        return NULL;
    }

    size_t pos = 0;
    buf[pos++] = '{';

    int first = 1;
    size_t last_complete_pos = 1;

#if PY_VERSION_HEX < 0x030D0000
    PyObject *key, *value;
    Py_ssize_t dict_pos = 0;
    while (PyDict_Next(locals, &dict_pos, &key, &value)) {
        const char *key_str = PyUnicode_AsUTF8(key);
        if (!key_str) { PyErr_Clear(); continue; }
        if (!serialize_one_local(key_str, value, buf, buf_size, &pos, &first, &last_complete_pos))
            break;
    }
#else
    PyObject *items = PyMapping_Items(locals);
    if (items) {
        Py_ssize_t n = PyList_GET_SIZE(items);
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *pair = PyList_GET_ITEM(items, i);
            PyObject *key = PyTuple_GET_ITEM(pair, 0);
            PyObject *value = PyTuple_GET_ITEM(pair, 1);
            const char *key_str = PyUnicode_AsUTF8(key);
            if (!key_str) { PyErr_Clear(); continue; }
            if (!serialize_one_local(key_str, value, buf, buf_size, &pos, &first, &last_complete_pos))
                break;
        }
        Py_DECREF(items);
    } else {
        PyErr_Clear();
    }
#endif

    /* Add extra key/value (e.g. __return__) if provided */
    if (extra_key && extra_val) {
        const char *ek = PyUnicode_AsUTF8(extra_key);
        if (ek) {
            if (!serialize_one_local(ek, extra_val, buf, buf_size, &pos, &first, &last_complete_pos)) {
                /* buffer full — fall through to truncation handling */
            }
        }
    }

    Py_DECREF(locals);

    if (pos + 1 >= buf_size) {
        pos = last_complete_pos;
        buf[pos++] = '}';
        buf[pos] = '\0';
        return buf;
    }
    buf[pos++] = '}';
    buf[pos] = '\0';
    return buf;
}

/* Keep static alias for internal use */
static const char *serialize_locals(PyObject *frame_obj, char *buf, size_t buf_size,
                                    PyObject *extra_key, PyObject *extra_val) {
    return recorder_serialize_locals(frame_obj, buf, buf_size, extra_key, extra_val);
}

/* ---- Trace Function ---- */

char g_locals_buf[MAX_LOCALS_JSON_SIZE];

/* Forward declaration for fast-forward functions */
#ifdef PYTTD_HAS_FORK
static int pyttd_trace_func_fast_forward(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg);
#endif

static int pyttd_trace_func(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg) {
    (void)obj;

#ifdef PYTTD_HAS_FORK
    if (g_fast_forward) {
        return pyttd_trace_func_fast_forward(obj, frame, what, arg);
    }
#endif

    if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) return 0;

    /* Check stop request in trace function (not just eval hook) so that
     * request_stop() can interrupt tight loops within a single frame.
     * Use atomic exchange to clear the flag — fire only once, so the
     * KeyboardInterrupt doesn't repeat in except/finally handlers. */
    if (what == PyTrace_LINE &&
        PyThread_get_thread_ident() == g_main_thread_id &&
        atomic_exchange_explicit(&g_stop_requested, 0, memory_order_relaxed)) {
        PyErr_SetNone(PyExc_KeyboardInterrupt);
        return -1;
    }

    switch (what) {
    case PyTrace_CALL:
        /* Skip — eval hook already recorded the call event */
        return 0;

    case PyTrace_LINE: {
        int line_no = PyFrame_GetLineNumber(frame);
        PyCodeObject *code = PyFrame_GetCode(frame);
        const char *filename = PyUnicode_AsUTF8(code->co_filename);
        const char *funcname = PyUnicode_AsUTF8(code->co_qualname);
        if (!filename || !funcname) { PyErr_Clear(); Py_DECREF(code); return 0; }

        const char *locals_json = serialize_locals((PyObject *)frame, g_locals_buf, sizeof(g_locals_buf), NULL, NULL);

        FrameEvent event;
        event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.thread_id = PyThread_get_thread_ident();
        event.timestamp = get_monotonic_time() - g_start_time;
        event.event_type = "line";
        event.filename = filename;
        event.function_name = funcname;
        event.locals_json = locals_json;

        ringbuf_push(&event);
        atomic_fetch_add_explicit(&g_frame_count, 1, memory_order_relaxed);
        Py_DECREF(code);

        /* Checkpoint trigger on line events (not just call events in eval hook).
         * With internal frames filtered, call events are too infrequent. */
#ifdef PYTTD_HAS_FORK
        if (g_checkpoint_interval > 0 &&
            g_checkpoint_callback != NULL &&
            !g_in_checkpoint &&
            event.sequence_no > 0 &&
            (event.sequence_no - atomic_load_explicit(&g_last_checkpoint_seq, memory_order_relaxed))
                >= (uint64_t)g_checkpoint_interval) {
            /* Skip checkpoint if non-main threads have been observed */
            if (ringbuf_thread_count() <= 1) {
            atomic_store_explicit(&g_last_checkpoint_seq, event.sequence_no, memory_order_relaxed);
            g_in_checkpoint = 1;
            checkpoint_do_fork(event.sequence_no, g_checkpoint_callback);
            g_in_checkpoint = 0;
            }
        }
#endif

        /* Signal flush thread if buffer is 75% full */
        if (ringbuf_fill_percent() >= 75) {
#ifndef _WIN32
            pthread_cond_signal(&g_flush_cond);
#endif
        }
        return 0;
    }

    case PyTrace_RETURN: {
        if (arg == NULL) {
            /* Exception propagation — handled by eval hook as exception_unwind */
            return 0;
        }

        PyCodeObject *code = PyFrame_GetCode(frame);
        int line_no = PyFrame_GetLineNumber(frame);
        const char *filename = PyUnicode_AsUTF8(code->co_filename);
        const char *funcname = PyUnicode_AsUTF8(code->co_qualname);
        if (!filename || !funcname) { PyErr_Clear(); Py_DECREF(code); return 0; }

        PyObject *return_key = PyUnicode_FromString("__return__");
        const char *locals_json = serialize_locals((PyObject *)frame, g_locals_buf, sizeof(g_locals_buf), return_key, arg);
        Py_XDECREF(return_key);

        FrameEvent event;
        event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.thread_id = PyThread_get_thread_ident();
        event.timestamp = get_monotonic_time() - g_start_time;
        event.event_type = "return";
        event.filename = filename;
        event.function_name = funcname;
        event.locals_json = locals_json;

        ringbuf_push(&event);
        atomic_fetch_add_explicit(&g_frame_count, 1, memory_order_relaxed);
        Py_DECREF(code);
        return 0;
    }

    case PyTrace_EXCEPTION: {
        PyCodeObject *code = PyFrame_GetCode(frame);
        int line_no = PyFrame_GetLineNumber(frame);
        const char *filename = PyUnicode_AsUTF8(code->co_filename);
        const char *funcname = PyUnicode_AsUTF8(code->co_qualname);
        if (!filename || !funcname) { PyErr_Clear(); Py_DECREF(code); return 0; }

        PyObject *exc_key = PyUnicode_FromString("__exception__");
        PyObject *exc_value = NULL;
        if (arg && PyTuple_Check(arg) && PyTuple_GET_SIZE(arg) >= 2) {
            exc_value = PyTuple_GET_ITEM(arg, 1);
        }
        const char *locals_json = serialize_locals((PyObject *)frame, g_locals_buf, sizeof(g_locals_buf), exc_key, exc_value);
        Py_XDECREF(exc_key);

        FrameEvent event;
        event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.thread_id = PyThread_get_thread_ident();
        event.timestamp = get_monotonic_time() - g_start_time;
        event.event_type = "exception";
        event.filename = filename;
        event.function_name = funcname;
        event.locals_json = locals_json;

        ringbuf_push(&event);
        atomic_fetch_add_explicit(&g_frame_count, 1, memory_order_relaxed);
        Py_DECREF(code);
        return 0;
    }

    default:
        return 0;
    }
}

/* ---- Fast-Forward Trace Function (Phase 2) ---- */

#ifdef PYTTD_HAS_FORK

/* Forward declarations */
int checkpoint_wait_for_command(int cmd_fd);
int serialize_target_state(int result_fd, int event_type, PyObject *trace_arg);

static int pyttd_trace_func_fast_forward(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg) {
    (void)obj;

    switch (what) {
    case PyTrace_CALL:
        /* Skip — eval hook already counted it */
        return 0;

    case PyTrace_LINE: {
        uint64_t seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        if (seq == g_fast_forward_target) {
            serialize_target_state(g_result_fd, -1, NULL);
            checkpoint_wait_for_command(g_cmd_fd);
        }
        return 0;
    }

    case PyTrace_RETURN: {
        if (arg == NULL) return 0;  /* exception propagation */
        uint64_t seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        if (seq == g_fast_forward_target) {
            serialize_target_state(g_result_fd, PyTrace_RETURN, arg);
            checkpoint_wait_for_command(g_cmd_fd);
        }
        return 0;
    }

    case PyTrace_EXCEPTION: {
        uint64_t seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        if (seq == g_fast_forward_target) {
            serialize_target_state(g_result_fd, PyTrace_EXCEPTION, arg);
            checkpoint_wait_for_command(g_cmd_fd);
        }
        return 0;
    }

    default:
        return 0;
    }
}

#endif /* PYTTD_HAS_FORK */

/* ---- PEP 523 Frame Eval Hook ---- */

#ifdef PYTTD_HAS_FORK
/* Forward declaration */
static PyObject *pyttd_eval_hook_fast_forward(PyThreadState *tstate,
                                               struct _PyInterpreterFrame *iframe,
                                               int throwflag);
#endif

static PyObject *pyttd_eval_hook(PyThreadState *tstate,
                                  struct _PyInterpreterFrame *iframe,
                                  int throwflag) {
#ifdef PYTTD_HAS_FORK
    /* Fast-forward check BEFORE g_recording check */
    if (g_fast_forward) {
        return pyttd_eval_hook_fast_forward(tstate, iframe, throwflag);
    }
#endif

    /* Skip recording if not active */
    if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) {
        return g_original_eval(tstate, iframe, throwflag);
    }

    /* Inside repr/IO callback — suppress recording and remove inherited trace
     * to prevent trace function from recording line/return events in this frame */
    if (g_inside_repr) {
        Py_tracefunc saved_trace = tstate->c_tracefunc;
        PyObject *saved_traceobj = tstate->c_traceobj;
        Py_XINCREF(saved_traceobj);
        if (saved_trace) {
            PyEval_SetTrace(NULL, NULL);
        }
        PyObject *result = g_original_eval(tstate, iframe, throwflag);
        if (saved_trace) {
            PyEval_SetTrace(saved_trace, saved_traceobj);
        }
        Py_XDECREF(saved_traceobj);
        return result;
    }

    /* Check stop request — only interrupt the main thread, not the flush thread.
     * Use atomic exchange to clear the flag — fire only once. */
    if (PyThread_get_thread_ident() == g_main_thread_id &&
        atomic_exchange_explicit(&g_stop_requested, 0, memory_order_relaxed)) {
        PyErr_SetNone(PyExc_KeyboardInterrupt);
        return NULL;
    }

    /* Get code object */
    PyCodeObject *code = PyUnstable_InterpreterFrame_GetCode(iframe);
    const char *filename = PyUnicode_AsUTF8(code->co_filename);
    const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

    /* If UTF-8 conversion failed, treat as ignored and skip */
    if (!filename || !funcname) {
        PyErr_Clear();
        Py_DECREF(code);
        return g_original_eval(tstate, iframe, throwflag);
    }

    /* Check ignore filter */
    if (should_ignore(filename, funcname)) {
        /* Save current trace, remove it, eval, restore.
         * Only call PyEval_SetTrace if trace is actually set
         * to avoid events counter overflow (Python 3.13+). */
        Py_tracefunc saved_trace = tstate->c_tracefunc;
        PyObject *saved_traceobj = tstate->c_traceobj;
        Py_XINCREF(saved_traceobj);

        if (saved_trace) {
            PyEval_SetTrace(NULL, NULL);
        }

        PyObject *result = g_original_eval(tstate, iframe, throwflag);

        /* Restore previous trace (only if we removed it) */
        if (saved_trace) {
            PyEval_SetTrace(saved_trace, saved_traceobj);
        }
        Py_XDECREF(saved_traceobj);
        Py_DECREF(code);
        return result;
    }

    /* Ensure per-thread ring buffer exists (lazy allocation on first frame) */
    if (!ringbuf_get_thread_buffer()) {
        ringbuf_get_or_create(PyThread_get_thread_ident());
    }

    /* Record call event */
    g_call_depth++;
    int line_no = PyUnstable_InterpreterFrame_GetLine(iframe);

    FrameEvent call_event;
    call_event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
    call_event.line_no = line_no;
    call_event.call_depth = g_call_depth;
    call_event.thread_id = PyThread_get_thread_ident();
    call_event.timestamp = get_monotonic_time() - g_start_time;
    call_event.event_type = "call";
    call_event.filename = filename;
    call_event.function_name = funcname;
    call_event.locals_json = NULL;

    ringbuf_push(&call_event);
    atomic_fetch_add_explicit(&g_frame_count, 1, memory_order_relaxed);

    /* Phase 2: Checkpoint trigger (delta-based, guarded against recursion) */
#ifdef PYTTD_HAS_FORK
    if (g_checkpoint_interval > 0 &&
        g_checkpoint_callback != NULL &&
        !g_in_checkpoint &&
        call_event.sequence_no > 0 &&
        (call_event.sequence_no - atomic_load_explicit(&g_last_checkpoint_seq, memory_order_relaxed))
            >= (uint64_t)g_checkpoint_interval) {
        /* Skip checkpoint if non-main threads have been observed */
        if (ringbuf_thread_count() <= 1) {
        atomic_store_explicit(&g_last_checkpoint_seq, call_event.sequence_no, memory_order_relaxed);
        g_in_checkpoint = 1;
        checkpoint_do_fork(call_event.sequence_no, g_checkpoint_callback);
        g_in_checkpoint = 0;
        }
    }
#endif

    /* Save current trace function, install ours.
     * Optimization: skip if our trace is already installed to avoid
     * PyEval_SetTrace's internal events counter overflow (Python 3.13+). */
    Py_tracefunc saved_trace = tstate->c_tracefunc;
    PyObject *saved_traceobj = tstate->c_traceobj;
    Py_XINCREF(saved_traceobj);

    int trace_changed = (saved_trace != (Py_tracefunc)pyttd_trace_func);
    if (trace_changed) {
        PyEval_SetTrace(pyttd_trace_func, NULL);
    }

    /* Call original eval */
    PyObject *result = g_original_eval(tstate, iframe, throwflag);

    /* Check for exception_unwind (eval returned NULL with exception) */
    if (result == NULL && PyErr_Occurred()) {
        FrameEvent unwind_event;
        unwind_event.sequence_no = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        unwind_event.line_no = line_no;
        unwind_event.call_depth = g_call_depth;
        unwind_event.thread_id = PyThread_get_thread_ident();
        unwind_event.timestamp = get_monotonic_time() - g_start_time;
        unwind_event.event_type = "exception_unwind";
        unwind_event.filename = filename;
        unwind_event.function_name = funcname;
        unwind_event.locals_json = NULL;

        ringbuf_push(&unwind_event);
        atomic_fetch_add_explicit(&g_frame_count, 1, memory_order_relaxed);
    }

    /* Decrement call_depth */
    g_call_depth--;

    /* Restore previous trace function (only if we changed it) */
    if (trace_changed) {
        if (saved_trace) {
            PyEval_SetTrace(saved_trace, saved_traceobj);
        } else {
            PyEval_SetTrace(NULL, NULL);
        }
    }
    Py_XDECREF(saved_traceobj);
    Py_DECREF(code);

    return result;
}

/* ---- Fast-Forward Eval Hook (Phase 2) ---- */

#ifdef PYTTD_HAS_FORK

void serialize_error_result(int result_fd, const char *error_code, uint64_t last_seq);

static PyObject *pyttd_eval_hook_fast_forward(PyThreadState *tstate,
                                               struct _PyInterpreterFrame *iframe,
                                               int throwflag) {
    /* Get code object */
    PyCodeObject *code = PyUnstable_InterpreterFrame_GetCode(iframe);
    const char *filename = PyUnicode_AsUTF8(code->co_filename);
    const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

    if (!filename || !funcname) {
        PyErr_Clear();
        Py_DECREF(code);
        return g_original_eval(tstate, iframe, throwflag);
    }

    /* Check ignore filter */
    if (should_ignore(filename, funcname)) {
        Py_tracefunc saved_trace = tstate->c_tracefunc;
        PyObject *saved_traceobj = tstate->c_traceobj;
        Py_XINCREF(saved_traceobj);

        if (saved_trace) {
            PyEval_SetTrace(NULL, NULL);
        }

        PyObject *result = g_original_eval(tstate, iframe, throwflag);

        if (saved_trace) {
            PyEval_SetTrace(saved_trace, saved_traceobj);
        }
        Py_XDECREF(saved_traceobj);
        Py_DECREF(code);
        return result;
    }

    /* Check thread */
    if (PyThread_get_thread_ident() != g_main_thread_id) {
        Py_DECREF(code);
        return g_original_eval(tstate, iframe, throwflag);
    }

    /* Count call event (no serialization/ringbuf in fast-forward) */
    g_call_depth++;
    uint64_t call_seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);

    /* Check if target reached at call event */
    if (call_seq == g_fast_forward_target) {
        serialize_target_state(g_result_fd, -1, NULL);
        checkpoint_wait_for_command(g_cmd_fd);
    }

    /* Install trace function for line/return/exception counting.
     * Optimization: skip if our trace is already installed. */
    Py_tracefunc saved_trace = tstate->c_tracefunc;
    PyObject *saved_traceobj = tstate->c_traceobj;
    Py_XINCREF(saved_traceobj);

    if (saved_trace != (Py_tracefunc)pyttd_trace_func_fast_forward) {
        PyEval_SetTrace(pyttd_trace_func_fast_forward, NULL);
    }

    /* Call original eval */
    PyObject *result = g_original_eval(tstate, iframe, throwflag);

    /* Check for exception_unwind */
    if (result == NULL && PyErr_Occurred()) {
        uint64_t unwind_seq = atomic_fetch_add_explicit(&g_sequence_counter, 1, memory_order_relaxed);
        if (unwind_seq == g_fast_forward_target) {
            serialize_target_state(g_result_fd, -1, NULL);
            checkpoint_wait_for_command(g_cmd_fd);
        }
    }

    g_call_depth--;

    /* Restore trace */
    if (saved_trace != (Py_tracefunc)pyttd_trace_func_fast_forward) {
        if (saved_trace) {
            PyEval_SetTrace(saved_trace, saved_traceobj);
        } else {
            PyEval_SetTrace(NULL, NULL);
        }
    }
    Py_XDECREF(saved_traceobj);
    Py_DECREF(code);

    /* Check if script ended during fast-forward */
    if (g_fast_forward && g_call_depth < 0) {
        uint64_t cur_seq = atomic_load_explicit(&g_sequence_counter, memory_order_relaxed);
        if (cur_seq <= g_fast_forward_target) {
            serialize_error_result(g_result_fd, "target_seq_unreachable",
                                   cur_seq);
        }
        /* Permanent loop — only DIE exits (via _exit) */
        while (1) {
            checkpoint_wait_for_command(g_cmd_fd);
            serialize_error_result(g_result_fd, "script_completed",
                                   atomic_load_explicit(&g_sequence_counter, memory_order_relaxed));
        }
    }

    return result;
}

#endif /* PYTTD_HAS_FORK */

/* ---- Flush Thread ---- */

static void flush_one_buffer(ThreadRingBuffer *rb) {
    static FrameEvent batch[FLUSH_BATCH_SIZE];
    uint32_t count = 0;

    ringbuf_pop_batch_from(rb, batch, FLUSH_BATCH_SIZE, &count);
    if (count == 0) {
        if (rb->orphaned) {
            rb->initialized = 0;  /* mark for cleanup */
        }
        return;
    }

    if (!atomic_load_explicit(&g_interpreter_alive, memory_order_relaxed)) {
        return;
    }

    PyGILState_STATE gstate = PyGILState_Ensure();

    /* Swap pools inside GIL to serialize with producer (which also holds GIL) */
    ringbuf_pool_swap_for(rb);

    /* Build Python list of dicts */
    PyObject *list = PyList_New(count);
    if (!list) {
        PyErr_WriteUnraisable(g_flush_callback);
        PyErr_Clear();
        ringbuf_pool_reset_consumer_for(rb);
        PyGILState_Release(gstate);
        return;
    }

    for (uint32_t i = 0; i < count; i++) {
        FrameEvent *e = &batch[i];
        PyObject *dict = PyDict_New();
        if (!dict) {
            Py_DECREF(list);
            PyErr_WriteUnraisable(g_flush_callback);
            PyErr_Clear();
            ringbuf_pool_reset_consumer_for(rb);
            PyGILState_Release(gstate);
            return;
        }

        PyObject *seq = PyLong_FromUnsignedLongLong(e->sequence_no);
        PyObject *ts = PyFloat_FromDouble(e->timestamp);
        PyObject *ln = PyLong_FromLong(e->line_no);
        PyObject *fn = e->filename ? PyUnicode_FromString(e->filename) : PyUnicode_FromString("");
        PyObject *func = e->function_name ? PyUnicode_FromString(e->function_name) : PyUnicode_FromString("");
        PyObject *evt = PyUnicode_FromString(e->event_type);
        PyObject *depth = PyLong_FromLong(e->call_depth);
        PyObject *locals_obj = e->locals_json ? PyUnicode_FromString(e->locals_json) : Py_NewRef(Py_None);
        PyObject *tid = PyLong_FromUnsignedLong(e->thread_id);

        if (!seq || !ts || !ln || !fn || !func || !evt || !depth || !locals_obj || !tid) {
            Py_XDECREF(seq); Py_XDECREF(ts); Py_XDECREF(ln);
            Py_XDECREF(fn); Py_XDECREF(func); Py_XDECREF(evt);
            Py_XDECREF(depth); Py_XDECREF(locals_obj); Py_XDECREF(tid);
            Py_DECREF(dict);
            PyErr_Clear();
            PyObject *empty = PyDict_New();
            PyList_SET_ITEM(list, i, empty ? empty : Py_NewRef(Py_None));
            continue;
        }

        if (PyDict_SetItemString(dict, "sequence_no", seq) < 0 ||
            PyDict_SetItemString(dict, "timestamp", ts) < 0 ||
            PyDict_SetItemString(dict, "line_no", ln) < 0 ||
            PyDict_SetItemString(dict, "filename", fn) < 0 ||
            PyDict_SetItemString(dict, "function_name", func) < 0 ||
            PyDict_SetItemString(dict, "frame_event", evt) < 0 ||
            PyDict_SetItemString(dict, "call_depth", depth) < 0 ||
            PyDict_SetItemString(dict, "locals_snapshot", locals_obj) < 0 ||
            PyDict_SetItemString(dict, "thread_id", tid) < 0) {
            PyErr_Clear();
        }

        Py_DECREF(seq); Py_DECREF(ts); Py_DECREF(ln);
        Py_DECREF(fn); Py_DECREF(func); Py_DECREF(evt);
        Py_DECREF(depth); Py_DECREF(locals_obj); Py_DECREF(tid);

        PyList_SET_ITEM(list, i, dict);
    }

    /* Call flush callback */
    PyObject *result = PyObject_CallOneArg(g_flush_callback, list);
    if (!result) {
        PyErr_WriteUnraisable(g_flush_callback);
        PyErr_Clear();
    } else {
        Py_DECREF(result);
    }
    Py_DECREF(list);
    atomic_fetch_add_explicit(&g_flush_count, 1, memory_order_relaxed);

    /* Reset consumer pool inside GIL to serialize with producer */
    ringbuf_pool_reset_consumer_for(rb);
    PyGILState_Release(gstate);
}

static void flush_batch(void) {
    /* Iterate all per-thread ring buffers */
    for (ThreadRingBuffer *rb = ringbuf_get_head(); rb != NULL; rb = rb->next) {
        if (!rb->initialized) continue;
        flush_one_buffer(rb);
    }
}

#ifdef _WIN32
static DWORD WINAPI flush_thread_func(LPVOID arg) {
    (void)arg;
    while (!atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) {
        Sleep(g_flush_interval_ms);
        flush_batch();
    }
    flush_batch();
    return 0;
}
#else
static void *flush_thread_func(void *arg) {
    (void)arg;
    while (!atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        long add_ns = (long)g_flush_interval_ms * 1000000L;
        ts.tv_sec += add_ns / 1000000000L;
        ts.tv_nsec += add_ns % 1000000000L;
        if (ts.tv_nsec >= 1000000000L) {
            ts.tv_sec += 1;
            ts.tv_nsec -= 1000000000L;
        }

        pthread_mutex_lock(&g_flush_mutex);
        pthread_cond_timedwait(&g_flush_cond, &g_flush_mutex, &ts);
        pthread_mutex_unlock(&g_flush_mutex);

        if (atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) break;
        flush_batch();

        /* Phase 2: Pre-fork synchronization — pause if requested */
        if (atomic_load_explicit(&g_pause_requested, memory_order_acquire)) {
            pthread_mutex_lock(&g_flush_mutex);
            atomic_store(&g_pause_acked, 1);
            pthread_cond_signal(&g_pause_ack_cv);
            while (atomic_load(&g_pause_requested)) {
                pthread_cond_wait(&g_resume_cv, &g_flush_mutex);
            }
            pthread_mutex_unlock(&g_flush_mutex);
        }
    }
    /* Final flush */
    flush_batch();

    /* Close flush thread's DB connection */
    if (atomic_load_explicit(&g_interpreter_alive, memory_order_relaxed)) {
        PyGILState_STATE gstate = PyGILState_Ensure();
        PyObject *base_mod = PyImport_ImportModule("pyttd.models.base");
        if (base_mod) {
            PyObject *db_obj = PyObject_GetAttrString(base_mod, "db");
            if (db_obj) {
                PyObject *close_result = PyObject_CallMethod(db_obj, "close", NULL);
                Py_XDECREF(close_result);
                if (PyErr_Occurred()) PyErr_Clear();
                Py_DECREF(db_obj);
            } else {
                PyErr_Clear();
            }
            Py_DECREF(base_mod);
        } else {
            PyErr_Clear();
        }
        PyGILState_Release(gstate);
    }
    return NULL;
}
#endif

/* ---- Python-Facing Functions ---- */

PyObject *pyttd_start_recording(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;

    if (atomic_load_explicit(&g_recording, memory_order_relaxed)) {
        PyErr_SetString(PyExc_RuntimeError, "Recording is already active");
        return NULL;
    }

    static char *kwlist[] = {"flush_callback", "buffer_size", "flush_interval_ms",
                             "checkpoint_callback", "checkpoint_interval",
                             "io_flush_callback", "io_replay_loader", NULL};
    PyObject *callback = NULL;
    int buffer_size = PYTTD_DEFAULT_CAPACITY;
    int flush_interval_ms = 10;
    PyObject *checkpoint_cb = NULL;
    int checkpoint_interval = 0;
    PyObject *io_flush_cb = NULL;
    PyObject *io_replay_loader = NULL;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|iiOiOO", kwlist,
                                      &callback, &buffer_size, &flush_interval_ms,
                                      &checkpoint_cb, &checkpoint_interval,
                                      &io_flush_cb, &io_replay_loader)) {
        return NULL;
    }

    if (!PyCallable_Check(callback)) {
        PyErr_SetString(PyExc_TypeError, "flush_callback must be callable");
        return NULL;
    }

    /* Store flush interval */
    g_flush_interval_ms = flush_interval_ms > 0 ? flush_interval_ms : 10;

    /* Validate buffer_size */
    if (buffer_size < 0 || (buffer_size > 0 && buffer_size < 64)) {
        PyErr_SetString(PyExc_ValueError, "buffer_size must be 0 (default) or >= 64");
        return NULL;
    }

    /* Validate checkpoint_interval */
    if (checkpoint_interval < 0) {
        PyErr_SetString(PyExc_ValueError, "checkpoint_interval must be >= 0");
        return NULL;
    }

    /* Initialize ring buffer system (per-thread buffers) */
    if (ringbuf_system_init((uint32_t)buffer_size) != PYTTD_RINGBUF_OK) {
        PyErr_SetString(PyExc_MemoryError, "Failed to initialize ring buffer");
        return NULL;
    }

    /* Reset counters */
    atomic_store_explicit(&g_sequence_counter, 0, memory_order_relaxed);
    g_call_depth = -1;  /* TLS — main thread */
    atomic_store_explicit(&g_frame_count, 0, memory_order_relaxed);
    atomic_store_explicit(&g_flush_count, 0, memory_order_relaxed);
    g_stop_time = 0.0;
    g_inside_repr = 0;
    g_fast_forward = 0;
    g_fast_forward_target = 0;
    atomic_store_explicit(&g_last_checkpoint_seq, 0, memory_order_relaxed);
    g_in_checkpoint = 0;
    g_cmd_fd = -1;
    g_result_fd = -1;
    g_saved_tstate = NULL;
    atomic_store_explicit(&g_stop_requested, 0, memory_order_relaxed);
    atomic_store_explicit(&g_flush_stop, 0, memory_order_relaxed);
    atomic_store_explicit(&g_interpreter_alive, 1, memory_order_relaxed);
#ifndef _WIN32
    atomic_store(&g_pause_requested, 0);
    atomic_store(&g_pause_acked, 0);
#endif

    /* Save flush callback */
    g_flush_callback = callback;
    Py_INCREF(g_flush_callback);

    /* Save checkpoint callback */
    g_checkpoint_callback = NULL;
    g_checkpoint_interval = 0;
    if (checkpoint_cb && checkpoint_cb != Py_None && PyCallable_Check(checkpoint_cb)) {
        g_checkpoint_callback = checkpoint_cb;
        Py_INCREF(g_checkpoint_callback);
    }
    if (checkpoint_interval > 0) {
        g_checkpoint_interval = checkpoint_interval;
    }

    /* Initialize checkpoint store */
#ifdef PYTTD_HAS_FORK
    checkpoint_store_init();
    /* Ignore SIGPIPE — writing to checkpoint pipes whose child has died
     * (read end closed) would otherwise terminate the process.
     * With SIG_IGN, write() returns -1/EPIPE which callers handle gracefully. */
    signal(SIGPIPE, SIG_IGN);
#endif

    /* Record main thread ID and start time */
    g_main_thread_id = PyThread_get_thread_ident();
    g_start_time = get_monotonic_time();

    /* Pre-create main thread's ring buffer */
    ringbuf_get_or_create(g_main_thread_id);

    /* Set environment variable so user scripts can detect recording mode */
#ifdef _WIN32
    _putenv_s("PYTTD_RECORDING", "1");
#else
    setenv("PYTTD_RECORDING", "1", 1);
#endif

    /* Install I/O hooks (before recording starts, non-fatal on failure) */
    if (io_flush_cb && io_flush_cb != Py_None) {
        install_io_hooks_internal(io_flush_cb, io_replay_loader);
    }

    /* Save original eval function and install our hook */
    PyInterpreterState *interp = PyInterpreterState_Get();
    g_original_eval = PYTTD_GET_EVAL_FUNC(interp);
    atomic_store_explicit(&g_recording, 1, memory_order_relaxed);
    PYTTD_SET_EVAL_FUNC(interp, pyttd_eval_hook);

    /* Start flush thread */
#ifdef _WIN32
    g_flush_thread = CreateThread(NULL, 0, flush_thread_func, NULL, 0, NULL);
    if (!g_flush_thread) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to create flush thread");
        atomic_store_explicit(&g_recording, 0, memory_order_relaxed);
        PYTTD_SET_EVAL_FUNC(interp, g_original_eval);
        remove_io_hooks_internal();
        ringbuf_destroy();
        Py_DECREF(g_flush_callback);
        g_flush_callback = NULL;
        Py_XDECREF(g_checkpoint_callback);
        g_checkpoint_callback = NULL;
        return NULL;
    }
#else
    g_flush_thread_created = 0;
    if (pthread_create(&g_flush_thread, NULL, flush_thread_func, NULL) != 0) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to create flush thread");
        atomic_store_explicit(&g_recording, 0, memory_order_relaxed);
        PYTTD_SET_EVAL_FUNC(interp, g_original_eval);
        remove_io_hooks_internal();
        ringbuf_destroy();
        Py_DECREF(g_flush_callback);
        g_flush_callback = NULL;
        Py_XDECREF(g_checkpoint_callback);
        g_checkpoint_callback = NULL;
        return NULL;
    }
    g_flush_thread_created = 1;
#endif
    Py_RETURN_NONE;
}

PyObject *pyttd_stop_recording(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;

    if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) {
        Py_RETURN_NONE;
    }

    g_stop_time = get_monotonic_time();
    atomic_store_explicit(&g_recording, 0, memory_order_relaxed);
    atomic_store_explicit(&g_stop_requested, 0, memory_order_relaxed);

    /* Clear recording environment variable */
#ifdef _WIN32
    _putenv_s("PYTTD_RECORDING", "");
#else
    unsetenv("PYTTD_RECORDING");
#endif

    /* Remove I/O hooks before flushing */
    remove_io_hooks_internal();

    /* Restore original eval function */
    PyInterpreterState *interp = PyInterpreterState_Get();
    PYTTD_SET_EVAL_FUNC(interp, g_original_eval);

    /* Remove trace function */
    PyEval_SetTrace(NULL, NULL);

    /* Stop flush thread */
    atomic_store_explicit(&g_flush_stop, 1, memory_order_relaxed);
#ifdef _WIN32
    if (g_flush_thread) {
        WaitForSingleObject(g_flush_thread, 5000);
        CloseHandle(g_flush_thread);
        g_flush_thread = NULL;
    }
#else
    if (g_flush_thread_created) {
        /* If flush thread is paused for fork, resume it first */
        pthread_mutex_lock(&g_flush_mutex);
        atomic_store(&g_pause_requested, 0);
        pthread_cond_signal(&g_resume_cv);
        pthread_cond_signal(&g_flush_cond);
        pthread_mutex_unlock(&g_flush_mutex);

        Py_BEGIN_ALLOW_THREADS
        pthread_join(g_flush_thread, NULL);
        Py_END_ALLOW_THREADS
        g_flush_thread_created = 0;
    }
#endif

    /* Destroy ring buffer system */
    ringbuf_system_destroy();

    /* Release callbacks — NOTE: do NOT kill checkpoint children here,
     * they're needed for replay */
    Py_XDECREF(g_flush_callback);
    g_flush_callback = NULL;
    Py_XDECREF(g_checkpoint_callback);
    g_checkpoint_callback = NULL;
    g_checkpoint_interval = 0;

    Py_RETURN_NONE;
}

PyObject *pyttd_get_recording_stats(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;

    RingbufStats rb_stats = ringbuf_get_stats();
    double elapsed = (g_stop_time > 0.0 ? g_stop_time : get_monotonic_time()) - g_start_time;

    PyObject *dict = PyDict_New();
    if (!dict) return NULL;

    PyObject *fc = PyLong_FromUnsignedLongLong(atomic_load_explicit(&g_frame_count, memory_order_relaxed));
    PyObject *df = PyLong_FromUnsignedLongLong(rb_stats.dropped_frames);
    PyObject *et = PyFloat_FromDouble(elapsed);
    PyObject *flc = PyLong_FromUnsignedLongLong(atomic_load_explicit(&g_flush_count, memory_order_relaxed));
    PyObject *po = PyLong_FromUnsignedLongLong(rb_stats.pool_overflows);

    if (!fc || !df || !et || !flc || !po) {
        Py_XDECREF(fc); Py_XDECREF(df); Py_XDECREF(et);
        Py_XDECREF(flc); Py_XDECREF(po);
        Py_DECREF(dict);
        return PyErr_NoMemory();
    }

    PyDict_SetItemString(dict, "frame_count", fc);
    PyDict_SetItemString(dict, "dropped_frames", df);
    PyDict_SetItemString(dict, "elapsed_time", et);
    PyDict_SetItemString(dict, "flush_count", flc);
    PyDict_SetItemString(dict, "pool_overflows", po);

    Py_DECREF(fc); Py_DECREF(df); Py_DECREF(et);
    Py_DECREF(flc); Py_DECREF(po);

    return dict;
}

PyObject *pyttd_set_ignore_patterns(PyObject *self, PyObject *args) {
    (void)self;

    PyObject *patterns_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &patterns_list)) {
        return NULL;
    }

    clear_filters();

    Py_ssize_t n = PyList_GET_SIZE(patterns_list);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = PyList_GET_ITEM(patterns_list, i);
        const char *pattern = PyUnicode_AsUTF8(item);
        if (!pattern) {
            PyErr_Clear();
            continue;
        }

        if (strchr(pattern, '/') != NULL || strchr(pattern, '\\') != NULL) {
            if (g_dir_filter.count < MAX_IGNORE_PATTERNS) {
                char *dup = strdup(pattern);
                if (dup) g_dir_filter.patterns[g_dir_filter.count++] = dup;
            }
        } else {
            if (g_exact_filter.count < MAX_IGNORE_PATTERNS * 2) {
                char *dup = strdup(pattern);
                if (dup) g_exact_filter.entries[g_exact_filter.count++] = dup;
            }
        }
    }

    Py_RETURN_NONE;
}

PyObject *pyttd_request_stop(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
    Py_RETURN_NONE;
}

PyObject *pyttd_set_recording_thread(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    g_main_thread_id = PyThread_get_thread_ident();
    Py_RETURN_NONE;
}
