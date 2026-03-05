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
#endif

#include "platform.h"
#include "frame_event.h"
#include "recorder.h"
#include "ringbuf.h"

/* ---- Version-gated macros for PEP 523 eval hook API ---- */

#if PY_VERSION_HEX >= 0x030F0000  /* 3.15+ */
  #define PYTTD_SET_EVAL_FUNC PyUnstable_InterpreterState_SetEvalFrameFunc
  #define PYTTD_GET_EVAL_FUNC PyUnstable_InterpreterState_GetEvalFrameFunc
#elif defined(_PyInterpreterState_SetEvalFrameFunc)
  #define PYTTD_SET_EVAL_FUNC _PyInterpreterState_SetEvalFrameFunc
  #define PYTTD_GET_EVAL_FUNC _PyInterpreterState_GetEvalFrameFunc
#else
  #define PYTTD_SET_EVAL_FUNC _PyInterpreterState_SetEvalFrameFunc
  #define PYTTD_GET_EVAL_FUNC _PyInterpreterState_GetEvalFrameFunc
#endif

/* Eval hook signature */
typedef PyObject *(*EvalFrameFunc)(PyThreadState *, struct _PyInterpreterFrame *, int);

/* ---- Maximum sizes ---- */
#define MAX_IGNORE_PATTERNS    64
#define MAX_REPR_LENGTH        256
#define MAX_LOCALS_JSON_SIZE   (64 * 1024)  /* 64KB per frame's locals JSON */
#define FLUSH_BATCH_SIZE       4096

/* ---- Global State ---- */

static int g_recording = 0;
static _Atomic int g_stop_requested = 0;
static _Atomic int g_interpreter_alive = 1;
static uint64_t g_sequence_counter = 0;
static int g_call_depth = -1;
static unsigned long g_main_thread_id = 0;
static EvalFrameFunc g_original_eval = NULL;
static PyObject *g_flush_callback = NULL;
static double g_start_time = 0.0;

/* ---- Stats ---- */
static uint64_t g_frame_count = 0;
static uint64_t g_flush_count = 0;

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
static HANDLE g_flush_mutex = NULL;
static HANDLE g_flush_event = NULL;
#else
static pthread_t g_flush_thread;
static int g_flush_thread_created = 0;
static pthread_mutex_t g_flush_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_flush_cond = PTHREAD_COND_INITIALIZER;
#endif
static _Atomic int g_flush_stop = 0;

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

/* Escape a string for JSON. Returns number of bytes written (excluding NUL),
 * or -1 if dst_size is insufficient. */
static int json_escape_string(const char *src, char *dst, size_t dst_size) {
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

/* ---- Locals Serialization ---- */

/* Serialize locals dict/proxy to JSON string in a buffer.
 * Returns the JSON string (pointer into buf), or NULL on failure.
 * The caller must Py_DECREF locals_obj after this call. */
static const char *serialize_locals(PyObject *frame_obj, char *buf, size_t buf_size,
                                    PyObject *extra_key, PyObject *extra_val) {
    PyObject *locals = PyFrame_GetLocals((PyFrameObject *)frame_obj);
    if (!locals) {
        PyErr_Clear();
        return NULL;
    }

    size_t pos = 0;
    buf[pos++] = '{';

    int first = 1;

#if PY_VERSION_HEX < 0x030D0000
    /* Python 3.12: PyFrame_GetLocals returns a dict, use PyDict_Next fast path */
    PyObject *key, *value;
    Py_ssize_t dict_pos = 0;
    while (PyDict_Next(locals, &dict_pos, &key, &value)) {
        const char *key_str = PyUnicode_AsUTF8(key);
        if (!key_str) { PyErr_Clear(); continue; }

        PyObject *repr = PyObject_Repr(value);
        if (!repr) { PyErr_Clear(); continue; }
        const char *repr_str = PyUnicode_AsUTF8(repr);
        if (!repr_str) { Py_DECREF(repr); PyErr_Clear(); continue; }

        /* Truncate repr if too long */
        char repr_truncated[MAX_REPR_LENGTH + 4];
        size_t repr_len = strlen(repr_str);
        if (repr_len > MAX_REPR_LENGTH) {
            memcpy(repr_truncated, repr_str, MAX_REPR_LENGTH);
            memcpy(repr_truncated + MAX_REPR_LENGTH, "...", 4);
            repr_str = repr_truncated;
        }

        if (!first) {
            if (pos + 2 >= buf_size) { Py_DECREF(repr); break; }
            buf[pos++] = ',';
            buf[pos++] = ' ';
        }
        first = 0;

        /* Write "key": "escaped_repr" */
        if (pos + 1 >= buf_size) { Py_DECREF(repr); break; }
        buf[pos++] = '"';
        int esc_len = json_escape_string(key_str, buf + pos, buf_size - pos - 10);
        if (esc_len < 0) { Py_DECREF(repr); break; }
        pos += esc_len;
        if (pos + 4 >= buf_size) { Py_DECREF(repr); break; }
        buf[pos++] = '"';
        buf[pos++] = ':';
        buf[pos++] = ' ';
        buf[pos++] = '"';
        esc_len = json_escape_string(repr_str, buf + pos, buf_size - pos - 3);
        if (esc_len < 0) { Py_DECREF(repr); break; }
        pos += esc_len;
        if (pos + 1 >= buf_size) { Py_DECREF(repr); break; }
        buf[pos++] = '"';
        Py_DECREF(repr);
    }
#else
    /* Python 3.13+: PyFrame_GetLocals returns FrameLocalsProxy, use PyMapping_Items */
    PyObject *items = PyMapping_Items(locals);
    if (items) {
        Py_ssize_t n = PyList_GET_SIZE(items);
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *pair = PyList_GET_ITEM(items, i);
            PyObject *key = PyTuple_GET_ITEM(pair, 0);
            PyObject *value = PyTuple_GET_ITEM(pair, 1);

            const char *key_str = PyUnicode_AsUTF8(key);
            if (!key_str) { PyErr_Clear(); continue; }

            PyObject *repr = PyObject_Repr(value);
            if (!repr) { PyErr_Clear(); continue; }
            const char *repr_str = PyUnicode_AsUTF8(repr);
            if (!repr_str) { Py_DECREF(repr); PyErr_Clear(); continue; }

            char repr_truncated[MAX_REPR_LENGTH + 4];
            size_t repr_len = strlen(repr_str);
            if (repr_len > MAX_REPR_LENGTH) {
                memcpy(repr_truncated, repr_str, MAX_REPR_LENGTH);
                memcpy(repr_truncated + MAX_REPR_LENGTH, "...", 4);
                repr_str = repr_truncated;
            }

            if (!first) {
                if (pos + 2 >= buf_size) { Py_DECREF(repr); break; }
                buf[pos++] = ',';
                buf[pos++] = ' ';
            }
            first = 0;

            if (pos + 1 >= buf_size) { Py_DECREF(repr); break; }
            buf[pos++] = '"';
            int esc_len = json_escape_string(key_str, buf + pos, buf_size - pos - 10);
            if (esc_len < 0) { Py_DECREF(repr); break; }
            pos += esc_len;
            if (pos + 4 >= buf_size) { Py_DECREF(repr); break; }
            buf[pos++] = '"';
            buf[pos++] = ':';
            buf[pos++] = ' ';
            buf[pos++] = '"';
            esc_len = json_escape_string(repr_str, buf + pos, buf_size - pos - 3);
            if (esc_len < 0) { Py_DECREF(repr); break; }
            pos += esc_len;
            if (pos + 1 >= buf_size) { Py_DECREF(repr); break; }
            buf[pos++] = '"';
            Py_DECREF(repr);
        }
        Py_DECREF(items);
    } else {
        PyErr_Clear();
    }
#endif

    /* Add extra key/value (e.g. __return__) if provided */
    if (extra_key && extra_val) {
        const char *ek = PyUnicode_AsUTF8(extra_key);
        PyObject *ev_repr = PyObject_Repr(extra_val);
        if (ek && ev_repr) {
            const char *ev_str = PyUnicode_AsUTF8(ev_repr);
            if (ev_str) {
                char ev_truncated[MAX_REPR_LENGTH + 4];
                size_t ev_len = strlen(ev_str);
                if (ev_len > MAX_REPR_LENGTH) {
                    memcpy(ev_truncated, ev_str, MAX_REPR_LENGTH);
                    memcpy(ev_truncated + MAX_REPR_LENGTH, "...", 4);
                    ev_str = ev_truncated;
                }
                if (!first) {
                    if (pos + 2 < buf_size) { buf[pos++] = ','; buf[pos++] = ' '; }
                }
                if (pos + 1 < buf_size) buf[pos++] = '"';
                int esc_len = json_escape_string(ek, buf + pos, buf_size - pos - 10);
                if (esc_len >= 0) {
                    pos += esc_len;
                    if (pos + 4 < buf_size) {
                        buf[pos++] = '"'; buf[pos++] = ':'; buf[pos++] = ' '; buf[pos++] = '"';
                        esc_len = json_escape_string(ev_str, buf + pos, buf_size - pos - 3);
                        if (esc_len >= 0) {
                            pos += esc_len;
                            if (pos + 1 < buf_size) buf[pos++] = '"';
                        }
                    }
                }
            }
        }
        Py_XDECREF(ev_repr);
    }

    Py_DECREF(locals);

    if (pos + 1 >= buf_size) {
        buf[buf_size - 2] = '}';
        buf[buf_size - 1] = '\0';
        return buf;
    }
    buf[pos++] = '}';
    buf[pos] = '\0';
    return buf;
}

/* ---- Trace Function ---- */

/* Thread-local JSON buffer for locals serialization */
static char g_locals_buf[MAX_LOCALS_JSON_SIZE];

static int pyttd_trace_func(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg) {
    (void)obj;

    if (!g_recording) return 0;

    switch (what) {
    case PyTrace_CALL:
        /* Skip — eval hook already recorded the call event */
        return 0;

    case PyTrace_LINE: {
        int line_no = PyFrame_GetLineNumber(frame);
        PyCodeObject *code = PyFrame_GetCode(frame);
        const char *filename = PyUnicode_AsUTF8(code->co_filename);
        const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

        const char *locals_json = serialize_locals((PyObject *)frame, g_locals_buf, sizeof(g_locals_buf), NULL, NULL);

        FrameEvent event;
        event.sequence_no = g_sequence_counter++;
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.timestamp = get_monotonic_time() - g_start_time;
        event.event_type = "line";
        event.filename = filename;
        event.function_name = funcname;
        event.locals_json = locals_json;

        ringbuf_push(&event);
        g_frame_count++;
        Py_DECREF(code);

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

        /* Create __return__ key */
        PyObject *return_key = PyUnicode_FromString("__return__");
        const char *locals_json = serialize_locals((PyObject *)frame, g_locals_buf, sizeof(g_locals_buf), return_key, arg);
        Py_XDECREF(return_key);

        FrameEvent event;
        event.sequence_no = g_sequence_counter++;
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.timestamp = get_monotonic_time() - g_start_time;
        event.event_type = "return";
        event.filename = filename;
        event.function_name = funcname;
        event.locals_json = locals_json;

        ringbuf_push(&event);
        g_frame_count++;
        Py_DECREF(code);
        return 0;
    }

    case PyTrace_EXCEPTION: {
        PyCodeObject *code = PyFrame_GetCode(frame);
        int line_no = PyFrame_GetLineNumber(frame);
        const char *filename = PyUnicode_AsUTF8(code->co_filename);
        const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

        /* arg is (type, value, traceback) — serialize repr(value) */
        PyObject *exc_key = PyUnicode_FromString("__exception__");
        PyObject *exc_value = NULL;
        if (arg && PyTuple_Check(arg) && PyTuple_GET_SIZE(arg) >= 2) {
            exc_value = PyTuple_GET_ITEM(arg, 1);
        }
        const char *locals_json = serialize_locals((PyObject *)frame, g_locals_buf, sizeof(g_locals_buf), exc_key, exc_value);
        Py_XDECREF(exc_key);

        FrameEvent event;
        event.sequence_no = g_sequence_counter++;
        event.line_no = line_no;
        event.call_depth = g_call_depth;
        event.timestamp = get_monotonic_time() - g_start_time;
        event.event_type = "exception";
        event.filename = filename;
        event.function_name = funcname;
        event.locals_json = locals_json;

        ringbuf_push(&event);
        g_frame_count++;
        Py_DECREF(code);
        return 0;
    }

    default:
        return 0;
    }
}

/* ---- PEP 523 Frame Eval Hook ---- */

static PyObject *pyttd_eval_hook(PyThreadState *tstate,
                                  struct _PyInterpreterFrame *iframe,
                                  int throwflag) {
    /* Check stop request */
    if (atomic_load_explicit(&g_stop_requested, memory_order_relaxed)) {
        PyErr_SetNone(PyExc_KeyboardInterrupt);
        return NULL;
    }

    /* Get code object */
    PyCodeObject *code = PyUnstable_InterpreterFrame_GetCode(iframe);
    const char *filename = PyUnicode_AsUTF8(code->co_filename);
    const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

    /* Check ignore filter */
    if (should_ignore(filename, funcname)) {
        /* Save current trace, remove it, eval, restore */
        Py_tracefunc saved_trace = tstate->c_tracefunc;
        PyObject *saved_traceobj = tstate->c_traceobj;
        Py_XINCREF(saved_traceobj);

        if (saved_trace) {
            PyEval_SetTrace(NULL, NULL);
        }

        PyObject *result = g_original_eval(tstate, iframe, throwflag);

        /* Restore previous trace */
        if (saved_trace) {
            PyEval_SetTrace(saved_trace, saved_traceobj);
        }
        Py_XDECREF(saved_traceobj);
        Py_DECREF(code);
        return result;
    }

    /* Check thread — only record main thread in Phase 1 */
    if (PyThread_get_thread_ident() != g_main_thread_id) {
        Py_DECREF(code);
        return g_original_eval(tstate, iframe, throwflag);
    }

    /* Record call event */
    g_call_depth++;
    int line_no = PyUnstable_InterpreterFrame_GetLine(iframe);

    FrameEvent call_event;
    call_event.sequence_no = g_sequence_counter++;
    call_event.line_no = line_no;
    call_event.call_depth = g_call_depth;
    call_event.timestamp = get_monotonic_time() - g_start_time;
    call_event.event_type = "call";
    call_event.filename = filename;
    call_event.function_name = funcname;
    call_event.locals_json = NULL;  /* call events don't capture locals */

    ringbuf_push(&call_event);
    g_frame_count++;

    /* Save current trace function, install ours */
    Py_tracefunc saved_trace = tstate->c_tracefunc;
    PyObject *saved_traceobj = tstate->c_traceobj;
    Py_XINCREF(saved_traceobj);

    PyEval_SetTrace(pyttd_trace_func, NULL);

    /* Call original eval */
    PyObject *result = g_original_eval(tstate, iframe, throwflag);

    /* Check for exception_unwind (eval returned NULL with exception) */
    if (result == NULL && PyErr_Occurred()) {
        /* Record exception_unwind BEFORE decrementing call_depth */
        FrameEvent unwind_event;
        unwind_event.sequence_no = g_sequence_counter++;
        unwind_event.line_no = line_no;
        unwind_event.call_depth = g_call_depth;
        unwind_event.timestamp = get_monotonic_time() - g_start_time;
        unwind_event.event_type = "exception_unwind";
        unwind_event.filename = filename;
        unwind_event.function_name = funcname;
        unwind_event.locals_json = NULL;

        ringbuf_push(&unwind_event);
        g_frame_count++;
    }

    /* Decrement call_depth (always, regardless of normal/exception exit) */
    g_call_depth--;

    /* Restore previous trace function */
    if (saved_trace) {
        PyEval_SetTrace(saved_trace, saved_traceobj);
    } else {
        PyEval_SetTrace(NULL, NULL);
    }
    Py_XDECREF(saved_traceobj);
    Py_DECREF(code);

    return result;
}

/* ---- Flush Thread ---- */

static void flush_batch(void) {
    static FrameEvent batch[FLUSH_BATCH_SIZE];
    uint32_t count = 0;

    ringbuf_pop_batch(batch, FLUSH_BATCH_SIZE, &count);
    if (count == 0) return;

    /* Swap pools so producer can write into new pool while we read from old */
    ringbuf_pool_swap();

    if (!atomic_load_explicit(&g_interpreter_alive, memory_order_relaxed)) {
        return;
    }

    PyGILState_STATE gstate = PyGILState_Ensure();

    /* Build Python list of dicts */
    PyObject *list = PyList_New(count);
    if (!list) {
        PyErr_WriteUnraisable(g_flush_callback);
        PyErr_Clear();
        PyGILState_Release(gstate);
        ringbuf_pool_reset_consumer();
        return;
    }

    for (uint32_t i = 0; i < count; i++) {
        FrameEvent *e = &batch[i];
        PyObject *dict = PyDict_New();
        if (!dict) {
            Py_DECREF(list);
            PyErr_WriteUnraisable(g_flush_callback);
            PyErr_Clear();
            PyGILState_Release(gstate);
            ringbuf_pool_reset_consumer();
            return;
        }

        PyObject *seq = PyLong_FromUnsignedLongLong(e->sequence_no);
        PyObject *ts = PyFloat_FromDouble(e->timestamp);
        PyObject *ln = PyLong_FromLong(e->line_no);
        PyObject *fn = e->filename ? PyUnicode_FromString(e->filename) : PyUnicode_FromString("");
        PyObject *func = e->function_name ? PyUnicode_FromString(e->function_name) : PyUnicode_FromString("");
        PyObject *evt = PyUnicode_FromString(e->event_type);
        PyObject *depth = PyLong_FromLong(e->call_depth);
        PyObject *locals = e->locals_json ? PyUnicode_FromString(e->locals_json) : Py_NewRef(Py_None);

        PyDict_SetItemString(dict, "sequence_no", seq);
        PyDict_SetItemString(dict, "timestamp", ts);
        PyDict_SetItemString(dict, "line_no", ln);
        PyDict_SetItemString(dict, "filename", fn);
        PyDict_SetItemString(dict, "function_name", func);
        PyDict_SetItemString(dict, "frame_event", evt);
        PyDict_SetItemString(dict, "call_depth", depth);
        PyDict_SetItemString(dict, "locals_snapshot", locals);

        Py_DECREF(seq); Py_DECREF(ts); Py_DECREF(ln);
        Py_DECREF(fn); Py_DECREF(func); Py_DECREF(evt);
        Py_DECREF(depth); Py_DECREF(locals);

        PyList_SET_ITEM(list, i, dict);  /* steals reference */
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
    g_flush_count++;

    PyGILState_Release(gstate);
    ringbuf_pool_reset_consumer();
}

#ifdef _WIN32
static DWORD WINAPI flush_thread_func(LPVOID arg) {
    (void)arg;
    while (!atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) {
        Sleep(10);
        flush_batch();
    }
    /* Final flush */
    flush_batch();
    return 0;
}
#else
static void *flush_thread_func(void *arg) {
    (void)arg;
    while (!atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        /* Add flush interval (default 10ms) */
        ts.tv_nsec += 10 * 1000000L;
        if (ts.tv_nsec >= 1000000000L) {
            ts.tv_sec += 1;
            ts.tv_nsec -= 1000000000L;
        }

        pthread_mutex_lock(&g_flush_mutex);
        pthread_cond_timedwait(&g_flush_cond, &g_flush_mutex, &ts);
        pthread_mutex_unlock(&g_flush_mutex);

        if (atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) break;
        flush_batch();
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

    if (g_recording) {
        PyErr_SetString(PyExc_RuntimeError, "Recording is already active");
        return NULL;
    }

    static char *kwlist[] = {"flush_callback", "buffer_size", "flush_interval_ms", NULL};
    PyObject *callback = NULL;
    int buffer_size = PYTTD_DEFAULT_CAPACITY;
    int flush_interval_ms = 10;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|ii", kwlist,
                                      &callback, &buffer_size, &flush_interval_ms)) {
        return NULL;
    }

    if (!PyCallable_Check(callback)) {
        PyErr_SetString(PyExc_TypeError, "flush_callback must be callable");
        return NULL;
    }

    /* Initialize ring buffer */
    if (ringbuf_init((uint32_t)buffer_size) != PYTTD_RINGBUF_OK) {
        PyErr_SetString(PyExc_MemoryError, "Failed to initialize ring buffer");
        return NULL;
    }

    /* Reset counters */
    g_sequence_counter = 0;
    g_call_depth = -1;
    g_frame_count = 0;
    g_flush_count = 0;
    atomic_store_explicit(&g_stop_requested, 0, memory_order_relaxed);
    atomic_store_explicit(&g_flush_stop, 0, memory_order_relaxed);
    atomic_store_explicit(&g_interpreter_alive, 1, memory_order_relaxed);

    /* Save flush callback */
    g_flush_callback = callback;
    Py_INCREF(g_flush_callback);

    /* Record main thread ID and start time */
    g_main_thread_id = PyThread_get_thread_ident();
    g_start_time = get_monotonic_time();

    /* Save original eval function and install our hook */
    PyInterpreterState *interp = PyInterpreterState_Get();
    g_original_eval = PYTTD_GET_EVAL_FUNC(interp);
    PYTTD_SET_EVAL_FUNC(interp, pyttd_eval_hook);

    /* Start flush thread */
#ifdef _WIN32
    g_flush_thread = CreateThread(NULL, 0, flush_thread_func, NULL, 0, NULL);
    if (!g_flush_thread) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to create flush thread");
        PYTTD_SET_EVAL_FUNC(interp, g_original_eval);
        ringbuf_destroy();
        Py_DECREF(g_flush_callback);
        g_flush_callback = NULL;
        return NULL;
    }
#else
    g_flush_thread_created = 0;
    if (pthread_create(&g_flush_thread, NULL, flush_thread_func, NULL) != 0) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to create flush thread");
        PYTTD_SET_EVAL_FUNC(interp, g_original_eval);
        ringbuf_destroy();
        Py_DECREF(g_flush_callback);
        g_flush_callback = NULL;
        return NULL;
    }
    g_flush_thread_created = 1;
#endif

    g_recording = 1;
    Py_RETURN_NONE;
}

PyObject *pyttd_stop_recording(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;

    if (!g_recording) {
        Py_RETURN_NONE;
    }

    g_recording = 0;

    /* Restore original eval function */
    PyInterpreterState *interp = PyInterpreterState_Get();
    PYTTD_SET_EVAL_FUNC(interp, g_original_eval);

    /* Remove trace function */
    PyEval_SetTrace(NULL, NULL);

    /* Stop flush thread */
    atomic_store_explicit(&g_flush_stop, 1, memory_order_relaxed);
#ifdef _WIN32
    if (g_flush_thread) {
        /* Signal and wait */
        WaitForSingleObject(g_flush_thread, 5000);
        CloseHandle(g_flush_thread);
        g_flush_thread = NULL;
    }
#else
    if (g_flush_thread_created) {
        pthread_cond_signal(&g_flush_cond);
        /* Release GIL while waiting for flush thread to finish
         * (it needs the GIL for its final flush + DB close) */
        Py_BEGIN_ALLOW_THREADS
        pthread_join(g_flush_thread, NULL);
        Py_END_ALLOW_THREADS
        g_flush_thread_created = 0;
    }
#endif

    /* Destroy ring buffer */
    ringbuf_destroy();

    /* Release callback */
    Py_XDECREF(g_flush_callback);
    g_flush_callback = NULL;

    Py_RETURN_NONE;
}

PyObject *pyttd_get_recording_stats(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;

    RingbufStats rb_stats = ringbuf_get_stats();
    double elapsed = get_monotonic_time() - g_start_time;

    PyObject *dict = PyDict_New();
    if (!dict) return NULL;

    PyObject *fc = PyLong_FromUnsignedLongLong(g_frame_count);
    PyObject *df = PyLong_FromUnsignedLongLong(rb_stats.dropped_frames);
    PyObject *et = PyFloat_FromDouble(elapsed);
    PyObject *flc = PyLong_FromUnsignedLongLong(g_flush_count);
    PyObject *po = PyLong_FromUnsignedLongLong(rb_stats.pool_overflows);

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

        /* Patterns containing '/' are directory/substring patterns */
        if (strchr(pattern, '/') != NULL) {
            if (g_dir_filter.count < MAX_IGNORE_PATTERNS) {
                g_dir_filter.patterns[g_dir_filter.count++] = strdup(pattern);
            }
        } else {
            if (g_exact_filter.count < MAX_IGNORE_PATTERNS * 2) {
                g_exact_filter.entries[g_exact_filter.count++] = strdup(pattern);
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
