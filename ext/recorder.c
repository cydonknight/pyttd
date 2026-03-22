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
#include <fnmatch.h>
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

/* Coroutine/generator detection flags from co_flags */
#ifndef CO_COROUTINE
#define CO_COROUTINE        0x0100
#endif
#ifndef CO_GENERATOR
#define CO_GENERATOR        0x0020
#endif
#ifndef CO_ASYNC_GENERATOR
#define CO_ASYNC_GENERATOR  0x0200
#endif
#define PYTTD_CORO_FLAGS (CO_COROUTINE | CO_GENERATOR | CO_ASYNC_GENERATOR)

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

static uint64_t g_max_frames = 0;  /* 0 = unlimited */

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

/* ---- Secrets Filter (Phase 8B) ---- */
static SubstringFilter g_secret_filter = {.count = 0};

/* ---- Include Filter (Phase 9B) ---- */
static SubstringFilter g_include_filter = {.count = 0};
static int g_include_mode = 0;  /* 1 if include patterns active */

/* ---- File Include Filter (P1-6) ---- */
static SubstringFilter g_file_include_filter = {.count = 0};
static int g_file_include_mode = 0;

/* ---- Exclude Filter (P1-6) ---- */
static SubstringFilter g_exclude_func_filter = {.count = 0};
static SubstringFilter g_exclude_file_filter = {.count = 0};
static int g_exclude_mode = 0;

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

/* ---- Secrets Filter (Phase 8B) ---- */

/* Case-insensitive substring search (portable — no strcasestr on Windows) */
static int ci_strstr(const char *haystack, const char *needle) {
    if (!haystack || !needle || !*needle) return 0;
    size_t nlen = strlen(needle);
    for (const char *h = haystack; *h; h++) {
        int match = 1;
        for (size_t i = 0; i < nlen; i++) {
            char hc = h[i];
            char nc = needle[i];
            if (!hc) { match = 0; break; }
            if (hc >= 'A' && hc <= 'Z') hc += 32;
            if (nc >= 'A' && nc <= 'Z') nc += 32;
            if (hc != nc) { match = 0; break; }
        }
        if (match) return 1;
    }
    return 0;
}

static int should_redact(const char *name) {
    for (int i = 0; i < g_secret_filter.count; i++) {
        if (ci_strstr(name, g_secret_filter.patterns[i])) {
            return 1;
        }
    }
    return 0;
}

static void clear_secret_filter(void) {
    for (int i = 0; i < g_secret_filter.count; i++) {
        free(g_secret_filter.patterns[i]);
        g_secret_filter.patterns[i] = NULL;
    }
    g_secret_filter.count = 0;
}

/* ---- Include Filter (Phase 9B) ---- */

static int has_glob_chars(const char *s) {
    for (; *s; s++) {
        if (*s == '*' || *s == '?' || *s == '[') return 1;
    }
    return 0;
}

static int should_include(const char *funcname) {
    if (!g_include_mode) return 1;
    /* <module> always included — top-level code must record */
    if (strcmp(funcname, "<module>") == 0) return 1;
    for (int i = 0; i < g_include_filter.count; i++) {
#ifndef _WIN32
        if (fnmatch(g_include_filter.patterns[i], funcname, 0) == 0) {
            return 1;
        }
#else
        if (strstr(funcname, g_include_filter.patterns[i]) != NULL) {
            return 1;
        }
#endif
    }
    return 0;
}

static void clear_include_filter(void) {
    for (int i = 0; i < g_include_filter.count; i++) {
        free(g_include_filter.patterns[i]);
        g_include_filter.patterns[i] = NULL;
    }
    g_include_filter.count = 0;
    g_include_mode = 0;
}

/* P1-6: File-path include filter */
static int should_include_file(const char *filename) {
    if (!g_file_include_mode) return 1;
    for (int i = 0; i < g_file_include_filter.count; i++) {
#ifndef _WIN32
        if (fnmatch(g_file_include_filter.patterns[i], filename, FNM_PATHNAME) == 0) {
            return 1;
        }
#else
        if (strstr(filename, g_file_include_filter.patterns[i]) != NULL) {
            return 1;
        }
#endif
    }
    return 0;
}

/* P1-6: Exclude filter (takes precedence over includes) */
static int should_exclude(const char *filename, const char *funcname) {
    if (!g_exclude_mode) return 0;
    /* Never exclude <module> — script entry must always record */
    if (strcmp(funcname, "<module>") == 0) return 0;
    for (int i = 0; i < g_exclude_func_filter.count; i++) {
#ifndef _WIN32
        if (fnmatch(g_exclude_func_filter.patterns[i], funcname, 0) == 0) return 1;
#else
        if (strstr(funcname, g_exclude_func_filter.patterns[i]) != NULL) return 1;
#endif
    }
    for (int i = 0; i < g_exclude_file_filter.count; i++) {
#ifndef _WIN32
        if (fnmatch(g_exclude_file_filter.patterns[i], filename, FNM_PATHNAME) == 0) return 1;
#else
        if (strstr(filename, g_exclude_file_filter.patterns[i]) != NULL) return 1;
#endif
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
    if (dst_size < 2) return -1;  /* need at least 1 char + NUL */
    size_t pos = 0;
    for (const char *p = src; *p; p++) {
        unsigned char c = (unsigned char)*p;
        if (c == '"') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = '"';
        } else if (c == '\\') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = '\\';
        } else if (c == '\n') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'n';
        } else if (c == '\r') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'r';
        } else if (c == '\t') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 't';
        } else if (c == '\b') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'b';
        } else if (c == '\f') {
            if (pos + 2 >= dst_size) return -1;
            dst[pos++] = '\\'; dst[pos++] = 'f';
        } else if (c < 0x20) {
            if (pos + 6 >= dst_size) return -1;
            pos += snprintf(dst + pos, dst_size - pos, "\\u%04x", c);
        } else {
            if (pos + 1 >= dst_size) return -1;
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

#define MAX_CHILDREN 50

/* Phase 10A: Detect if a value should be serialized as an expandable structure.
 * Only expand basic container types and user objects (not modules, types, functions, etc.) */
static int is_expandable(PyObject *value) {
    if (PyDict_Check(value) || PyList_Check(value) ||
        PyTuple_Check(value) || PySet_Check(value)) {
        return 1;
    }
    /* Skip modules, types, functions, methods, code objects */
    if (PyModule_Check(value) || PyType_Check(value) ||
        PyFunction_Check(value) || PyMethod_Check(value)) {
        return 0;
    }
    /* User objects with __dict__ (but not builtins) */
    if (PyObject_HasAttrString(value, "__dict__")) {
        PyObject *tp = (PyObject *)Py_TYPE(value);
        /* Skip types defined in C (builtins, extensions) — only expand Python classes */
        if (((PyTypeObject *)tp)->tp_flags & Py_TPFLAGS_HEAPTYPE) {
            return 1;
        }
    }
    return 0;
}

/* Write expandable structured JSON into buf at *pos.
 * Format: {"__type__":"dict","__len__":N,"__repr__":"...","__children__":[...]}
 * Returns 1 on success, 0 on buffer full (caller should fall back to flat repr). */
static int serialize_expandable_value(PyObject *value, char *buf, size_t buf_size,
                                       size_t *pos) {
    const char *type_name;
    Py_ssize_t length = 0;

    if (PyDict_Check(value)) {
        type_name = "dict";
        length = PyDict_Size(value);
    } else if (PyList_Check(value)) {
        type_name = "list";
        length = PyList_GET_SIZE(value);
    } else if (PyTuple_Check(value)) {
        type_name = "tuple";
        length = PyTuple_GET_SIZE(value);
    } else if (PySet_Check(value)) {
        type_name = "set";
        length = PySet_GET_SIZE(value);
    } else {
        type_name = "object";
    }

    /* Get repr */
    g_inside_repr = 1;
    PyObject *repr = PyObject_Repr(value);
    g_inside_repr = 0;
    if (!repr) { PyErr_Clear(); return 0; }
    const char *repr_str = PyUnicode_AsUTF8(repr);
    if (!repr_str) { Py_DECREF(repr); PyErr_Clear(); return 0; }

    char repr_truncated[MAX_REPR_LENGTH + 4];
    size_t repr_len = strlen(repr_str);
    if (repr_len > MAX_REPR_LENGTH) {
        memcpy(repr_truncated, repr_str, MAX_REPR_LENGTH);
        memcpy(repr_truncated + MAX_REPR_LENGTH, "...", 4);
        repr_str = repr_truncated;
    }

    /* Write: {"__type__":"<type>","__len__":<N>,"__repr__":"<repr>","__children__":[ */
    size_t needed = 80 + repr_len;
    if (*pos + needed >= buf_size) { Py_DECREF(repr); return 0; }

    int n = snprintf(buf + *pos, buf_size - *pos,
                     "{\"__type__\": \"%s\", \"__len__\": %zd, \"__repr__\": \"",
                     type_name, length);
    if (n < 0 || (size_t)n >= buf_size - *pos) { Py_DECREF(repr); return 0; }
    *pos += n;

    if (*pos + 30 >= buf_size) { Py_DECREF(repr); return 0; }
    int esc_len = json_escape_string(repr_str, buf + *pos, buf_size - *pos - 30);
    Py_DECREF(repr);
    if (esc_len < 0) return 0;
    *pos += esc_len;

    if (*pos + 21 >= buf_size) return 0;
    memcpy(buf + *pos, "\", \"__children__\": [", 20);
    *pos += 20;

    /* Serialize children (max MAX_CHILDREN, 1 level deep — children are flat repr) */
    int child_count = 0;
    int child_first = 1;

    if (PyDict_Check(value)) {
        PyObject *key, *val;
        Py_ssize_t dict_pos = 0;
        while (PyDict_Next(value, &dict_pos, &key, &val) && child_count < MAX_CHILDREN) {
            g_inside_repr = 1;
            PyObject *krepr = PyObject_Repr(key);
            PyObject *vrepr = PyObject_Repr(val);
            g_inside_repr = 0;
            if (!krepr || !vrepr) {
                Py_XDECREF(krepr); Py_XDECREF(vrepr);
                PyErr_Clear();
                continue;
            }
            const char *ks = PyUnicode_AsUTF8(krepr);
            const char *vs = PyUnicode_AsUTF8(vrepr);
            if (!ks || !vs) {
                Py_DECREF(krepr); Py_DECREF(vrepr);
                PyErr_Clear();
                continue;
            }
            if (!child_first) {
                if (*pos + 2 >= buf_size) { Py_DECREF(krepr); Py_DECREF(vrepr); break; }
                buf[(*pos)++] = ',';
                buf[(*pos)++] = ' ';
            }
            child_first = 0;
            /* {"key":"k","value":"v","type":"t"} */
            const char *vtype = val->ob_type->tp_name;
            if (*pos + 50 >= buf_size) { Py_DECREF(krepr); Py_DECREF(vrepr); break; }
            memcpy(buf + *pos, "{\"key\": \"", 9); *pos += 9;
            esc_len = json_escape_string(ks, buf + *pos, buf_size - *pos - 40);
            if (esc_len < 0) { Py_DECREF(krepr); Py_DECREF(vrepr); break; }
            *pos += esc_len;
            if (*pos + 44 >= buf_size) { Py_DECREF(krepr); Py_DECREF(vrepr); break; }
            memcpy(buf + *pos, "\", \"value\": \"", 13); *pos += 13;
            if (*pos + 30 >= buf_size) { Py_DECREF(krepr); Py_DECREF(vrepr); break; }
            esc_len = json_escape_string(vs, buf + *pos, buf_size - *pos - 30);
            Py_DECREF(krepr); Py_DECREF(vrepr);
            if (esc_len < 0) break;
            *pos += esc_len;
            n = snprintf(buf + *pos, buf_size - *pos, "\", \"type\": \"%s\"}", vtype);
            if (n < 0 || (size_t)n >= buf_size - *pos) break;
            *pos += n;
            child_count++;
        }
    } else if (PyList_Check(value) || PyTuple_Check(value)) {
        Py_ssize_t len = PyList_Check(value) ? PyList_GET_SIZE(value) : PyTuple_GET_SIZE(value);
        Py_ssize_t limit = len < MAX_CHILDREN ? len : MAX_CHILDREN;
        for (Py_ssize_t i = 0; i < limit; i++) {
            PyObject *item = PyList_Check(value) ? PyList_GET_ITEM(value, i) : PyTuple_GET_ITEM(value, i);
            g_inside_repr = 1;
            PyObject *irepr = PyObject_Repr(item);
            g_inside_repr = 0;
            if (!irepr) { PyErr_Clear(); continue; }
            const char *is = PyUnicode_AsUTF8(irepr);
            if (!is) { Py_DECREF(irepr); PyErr_Clear(); continue; }
            if (!child_first) {
                if (*pos + 2 >= buf_size) { Py_DECREF(irepr); break; }
                buf[(*pos)++] = ',';
                buf[(*pos)++] = ' ';
            }
            child_first = 0;
            const char *itype = item->ob_type->tp_name;
            n = snprintf(buf + *pos, buf_size - *pos, "{\"key\": \"%zd\", \"value\": \"", i);
            if (n < 0 || (size_t)n >= buf_size - *pos) { Py_DECREF(irepr); break; }
            *pos += n;
            if (*pos + 30 >= buf_size) { Py_DECREF(irepr); break; }
            esc_len = json_escape_string(is, buf + *pos, buf_size - *pos - 30);
            Py_DECREF(irepr);
            if (esc_len < 0) break;
            *pos += esc_len;
            n = snprintf(buf + *pos, buf_size - *pos, "\", \"type\": \"%s\"}", itype);
            if (n < 0 || (size_t)n >= buf_size - *pos) break;
            *pos += n;
            child_count++;
        }
    } else if (PySet_Check(value)) {
        PyObject *iter = PyObject_GetIter(value);
        if (iter) {
            PyObject *item;
            int idx = 0;
            while ((item = PyIter_Next(iter)) && child_count < MAX_CHILDREN) {
                g_inside_repr = 1;
                PyObject *irepr = PyObject_Repr(item);
                g_inside_repr = 0;
                if (!irepr) { Py_DECREF(item); PyErr_Clear(); continue; }
                const char *is = PyUnicode_AsUTF8(irepr);
                if (!is) { Py_DECREF(irepr); Py_DECREF(item); PyErr_Clear(); continue; }
                if (!child_first) {
                    if (*pos + 2 >= buf_size) { Py_DECREF(irepr); Py_DECREF(item); break; }
                    buf[(*pos)++] = ',';
                    buf[(*pos)++] = ' ';
                }
                child_first = 0;
                const char *itype = item->ob_type->tp_name;
                n = snprintf(buf + *pos, buf_size - *pos, "{\"key\": \"%d\", \"value\": \"", idx);
                if (n < 0 || (size_t)n >= buf_size - *pos) { Py_DECREF(irepr); Py_DECREF(item); break; }
                *pos += n;
                if (*pos + 30 >= buf_size) { Py_DECREF(irepr); Py_DECREF(item); break; }
                esc_len = json_escape_string(is, buf + *pos, buf_size - *pos - 30);
                Py_DECREF(irepr);
                Py_DECREF(item);
                if (esc_len < 0) break;
                *pos += esc_len;
                n = snprintf(buf + *pos, buf_size - *pos, "\", \"type\": \"%s\"}", itype);
                if (n < 0 || (size_t)n >= buf_size - *pos) break;
                *pos += n;
                child_count++;
                idx++;
            }
            Py_DECREF(iter);
            if (PyErr_Occurred()) PyErr_Clear();
        } else {
            PyErr_Clear();
        }
    } else {
        /* Object with __dict__ */
        PyObject *obj_dict = PyObject_GetAttrString(value, "__dict__");
        if (obj_dict && PyDict_Check(obj_dict)) {
            PyObject *key, *val;
            Py_ssize_t dict_pos = 0;
            while (PyDict_Next(obj_dict, &dict_pos, &key, &val) && child_count < MAX_CHILDREN) {
                const char *ks = PyUnicode_AsUTF8(key);
                if (!ks) { PyErr_Clear(); continue; }
                g_inside_repr = 1;
                PyObject *vrepr = PyObject_Repr(val);
                g_inside_repr = 0;
                if (!vrepr) { PyErr_Clear(); continue; }
                const char *vs = PyUnicode_AsUTF8(vrepr);
                if (!vs) { Py_DECREF(vrepr); PyErr_Clear(); continue; }
                if (!child_first) {
                    if (*pos + 2 >= buf_size) { Py_DECREF(vrepr); break; }
                    buf[(*pos)++] = ',';
                    buf[(*pos)++] = ' ';
                }
                child_first = 0;
                const char *vtype = val->ob_type->tp_name;
                if (*pos + 50 >= buf_size) { Py_DECREF(vrepr); break; }
                memcpy(buf + *pos, "{\"key\": \"", 9); *pos += 9;
                esc_len = json_escape_string(ks, buf + *pos, buf_size - *pos - 40);
                if (esc_len < 0) { Py_DECREF(vrepr); break; }
                *pos += esc_len;
                if (*pos + 44 >= buf_size) { Py_DECREF(vrepr); break; }
                memcpy(buf + *pos, "\", \"value\": \"", 13); *pos += 13;
                if (*pos + 30 >= buf_size) { Py_DECREF(vrepr); break; }
                esc_len = json_escape_string(vs, buf + *pos, buf_size - *pos - 30);
                Py_DECREF(vrepr);
                if (esc_len < 0) break;
                *pos += esc_len;
                n = snprintf(buf + *pos, buf_size - *pos, "\", \"type\": \"%s\"}", vtype);
                if (n < 0 || (size_t)n >= buf_size - *pos) break;
                *pos += n;
                child_count++;
            }
        }
        Py_XDECREF(obj_dict);
        if (PyErr_Occurred()) PyErr_Clear();
    }

    /* Close: ]} */
    if (*pos + 2 >= buf_size) return 0;
    buf[(*pos)++] = ']';
    buf[(*pos)++] = '}';
    return 1;
}

static int serialize_one_local(const char *key_str, PyObject *value,
                               char *buf, size_t buf_size,
                               size_t *pos, int *first, size_t *last_complete_pos) {
    /* Phase 8B: Secrets redaction — skip repr entirely for sensitive variables */
    if (should_redact(key_str)) {
        const char *redacted = "<redacted>";
        if (!*first) {
            if (*pos + 2 >= buf_size) return 0;
            buf[(*pos)++] = ',';
            buf[(*pos)++] = ' ';
        }
        *first = 0;
        /* Write "key": "<redacted>" */
        if (*pos + 12 >= buf_size) return 0;
        buf[(*pos)++] = '"';
        int esc_len = json_escape_string(key_str, buf + *pos, buf_size - *pos - 10);
        if (esc_len < 0) return 0;
        *pos += esc_len;
        if (*pos + 5 >= buf_size) return 0;
        buf[(*pos)++] = '"';
        buf[(*pos)++] = ':';
        buf[(*pos)++] = ' ';
        buf[(*pos)++] = '"';
        size_t rlen = strlen(redacted);
        if (*pos + rlen + 2 >= buf_size) return 0;
        memcpy(buf + *pos, redacted, rlen);
        *pos += rlen;
        buf[(*pos)++] = '"';
        *last_complete_pos = *pos;
        return 1;
    }

    /* Phase 10A: Try expandable serialization for container types */
    if (is_expandable(value)) {
        size_t save_pos = *pos;
        int save_first = *first;
        if (!*first) {
            if (*pos + 2 >= buf_size) return 0;
            buf[(*pos)++] = ',';
            buf[(*pos)++] = ' ';
        }
        *first = 0;
        /* Write "key": {structured...} */
        if (*pos + 12 >= buf_size) { *pos = save_pos; *first = save_first; goto flat_repr; }
        buf[(*pos)++] = '"';
        int esc_len = json_escape_string(key_str, buf + *pos, buf_size - *pos - 10);
        if (esc_len < 0) { *pos = save_pos; *first = save_first; goto flat_repr; }
        *pos += esc_len;
        if (*pos + 4 >= buf_size) { *pos = save_pos; *first = save_first; goto flat_repr; }
        buf[(*pos)++] = '"';
        buf[(*pos)++] = ':';
        buf[(*pos)++] = ' ';
        if (serialize_expandable_value(value, buf, buf_size, pos)) {
            *last_complete_pos = *pos;
            return 1;
        }
        /* Fall back to flat repr on buffer overflow */
        *pos = save_pos;
        *first = save_first;
    }

flat_repr:;
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
    int esc_len2 = json_escape_string(key_str, buf + *pos, buf_size - *pos - 10);
    if (esc_len2 < 0) { Py_DECREF(repr); return 0; }
    *pos += esc_len2;
    if (*pos + 5 >= buf_size) { Py_DECREF(repr); return 0; }
    buf[(*pos)++] = '"';
    buf[(*pos)++] = ':';
    buf[(*pos)++] = ' ';
    buf[(*pos)++] = '"';
    if (*pos + 4 >= buf_size) { Py_DECREF(repr); return 0; }
    int esc_len3 = json_escape_string(repr_str, buf + *pos, buf_size - *pos - 3);
    if (esc_len3 < 0) { Py_DECREF(repr); return 0; }
    *pos += esc_len3;
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
        event.is_coroutine = (code->co_flags & PYTTD_CORO_FLAGS) ? 1 : 0;

        ringbuf_push(&event);
        atomic_fetch_add_explicit(&g_frame_count, 1, memory_order_relaxed);

        /* P1-4: Auto-stop when max_frames reached (line events are most frequent) */
        if (g_max_frames > 0 && event.sequence_no >= g_max_frames) {
            atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
        }

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
        event.is_coroutine = (code->co_flags & PYTTD_CORO_FLAGS) ? 1 : 0;

        ringbuf_push(&event);
        atomic_fetch_add_explicit(&g_frame_count, 1, memory_order_relaxed);
        Py_DECREF(code);
        return 0;
    }

    case PyTrace_EXCEPTION: {
        /* Save active exception state — serialize_locals() has PyErr_Clear() paths
         * that can accidentally wipe the active exception on Python 3.12, breaking
         * exception_unwind detection in the eval hook's PyErr_Occurred() check */
        PyObject *save_type, *save_value, *save_tb;
        PyErr_Fetch(&save_type, &save_value, &save_tb);

        PyCodeObject *code = PyFrame_GetCode(frame);
        int line_no = PyFrame_GetLineNumber(frame);
        const char *filename = PyUnicode_AsUTF8(code->co_filename);
        const char *funcname = PyUnicode_AsUTF8(code->co_qualname);
        if (!filename || !funcname) {
            if (PyErr_Occurred()) PyErr_Clear();
            Py_DECREF(code);
            goto exc_restore;
        }

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
        event.is_coroutine = (code->co_flags & PYTTD_CORO_FLAGS) ? 1 : 0;

        ringbuf_push(&event);
        atomic_fetch_add_explicit(&g_frame_count, 1, memory_order_relaxed);
        Py_DECREF(code);

exc_restore:
        /* Discard any errors from recording, restore original exception */
        if (PyErr_Occurred()) PyErr_Clear();
        PyErr_Restore(save_type, save_value, save_tb);
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

    /* P1-6: Exclude filter — skip if excluded (takes precedence over includes) */
    if (should_exclude(filename, funcname)) {
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

    /* P1-6: File include filter */
    if (!should_include_file(filename)) {
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

    /* Phase 9B: Include filter — skip non-matching functions (same pattern as ignore) */
    if (!should_include(funcname)) {
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

    /* Ensure per-thread ring buffer exists (lazy allocation on first frame) */
    if (!ringbuf_get_thread_buffer()) {
        ringbuf_get_or_create(PyThread_get_thread_ident());
    }

    /* Record call event */
    g_call_depth++;
    int line_no = PyUnstable_InterpreterFrame_GetLine(iframe);
    int is_coro = (code->co_flags & PYTTD_CORO_FLAGS) ? 1 : 0;

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
    call_event.is_coroutine = is_coro;

    ringbuf_push(&call_event);
    atomic_fetch_add_explicit(&g_frame_count, 1, memory_order_relaxed);

    /* P1-4: Auto-stop when max_frames reached */
    if (g_max_frames > 0 && call_event.sequence_no >= g_max_frames) {
        atomic_store_explicit(&g_stop_requested, 1, memory_order_relaxed);
    }

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
        unwind_event.is_coroutine = is_coro;

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
        PyObject *is_coro = PyBool_FromLong(e->is_coroutine);

        if (!seq || !ts || !ln || !fn || !func || !evt || !depth || !locals_obj || !tid || !is_coro) {
            Py_XDECREF(seq); Py_XDECREF(ts); Py_XDECREF(ln);
            Py_XDECREF(fn); Py_XDECREF(func); Py_XDECREF(evt);
            Py_XDECREF(depth); Py_XDECREF(locals_obj); Py_XDECREF(tid);
            Py_XDECREF(is_coro);
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
            PyDict_SetItemString(dict, "thread_id", tid) < 0 ||
            PyDict_SetItemString(dict, "is_coroutine", is_coro) < 0) {
            PyErr_Clear();
        }

        Py_DECREF(seq); Py_DECREF(ts); Py_DECREF(ln);
        Py_DECREF(fn); Py_DECREF(func); Py_DECREF(evt);
        Py_DECREF(depth); Py_DECREF(locals_obj); Py_DECREF(tid);
        Py_DECREF(is_coro);

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
        if (atomic_load_explicit(&g_flush_stop, memory_order_relaxed)) break;
        flush_batch();
    }
    /* Final flush */
    flush_batch();

    /* Close flush thread's DB connection (same as Unix path) */
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
    g_max_frames = 0;
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
        Py_BEGIN_ALLOW_THREADS
        WaitForSingleObject(g_flush_thread, INFINITE);
        Py_END_ALLOW_THREADS
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

/* Phase 8B: Set secret patterns for variable redaction */
PyObject *pyttd_set_secret_patterns(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *patterns_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &patterns_list)) {
        return NULL;
    }
    clear_secret_filter();
    Py_ssize_t n = PyList_GET_SIZE(patterns_list);
    for (Py_ssize_t i = 0; i < n && g_secret_filter.count < MAX_IGNORE_PATTERNS; i++) {
        PyObject *item = PyList_GET_ITEM(patterns_list, i);
        const char *pattern = PyUnicode_AsUTF8(item);
        if (!pattern) { PyErr_Clear(); continue; }
        char *dup = strdup(pattern);
        if (dup) g_secret_filter.patterns[g_secret_filter.count++] = dup;
    }
    Py_RETURN_NONE;
}

/* Phase 9B: Set include patterns for selective recording */
PyObject *pyttd_set_include_patterns(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *patterns_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &patterns_list)) {
        return NULL;
    }
    clear_include_filter();
    Py_ssize_t n = PyList_GET_SIZE(patterns_list);
    if (n > 0) {
        g_include_mode = 1;
        for (Py_ssize_t i = 0; i < n && g_include_filter.count < MAX_IGNORE_PATTERNS; i++) {
            PyObject *item = PyList_GET_ITEM(patterns_list, i);
            const char *pattern = PyUnicode_AsUTF8(item);
            if (!pattern) { PyErr_Clear(); continue; }
#ifndef _WIN32
            if (!has_glob_chars(pattern)) {
                /* Auto-wrap plain substring patterns as *pattern* for fnmatch */
                size_t len = strlen(pattern);
                char *wrapped = (char *)malloc(len + 3);
                if (wrapped) {
                    snprintf(wrapped, len + 3, "*%s*", pattern);
                    g_include_filter.patterns[g_include_filter.count++] = wrapped;
                }
            } else {
                char *dup = strdup(pattern);
                if (dup) g_include_filter.patterns[g_include_filter.count++] = dup;
            }
#else
            char *dup = strdup(pattern);
            if (dup) g_include_filter.patterns[g_include_filter.count++] = dup;
#endif
        }
    }
    Py_RETURN_NONE;
}

PyObject *pyttd_set_max_frames(PyObject *self, PyObject *args) {
    (void)self;
    unsigned long long max_frames;
    if (!PyArg_ParseTuple(args, "K", &max_frames)) {
        return NULL;
    }
    g_max_frames = (uint64_t)max_frames;
    Py_RETURN_NONE;
}

PyObject *pyttd_set_file_include_patterns(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *patterns_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &patterns_list)) return NULL;
    /* Clear existing file include patterns */
    for (int i = 0; i < g_file_include_filter.count; i++) {
        free(g_file_include_filter.patterns[i]);
        g_file_include_filter.patterns[i] = NULL;
    }
    g_file_include_filter.count = 0;
    g_file_include_mode = 0;
    Py_ssize_t n = PyList_GET_SIZE(patterns_list);
    if (n > 0) {
        g_file_include_mode = 1;
        for (Py_ssize_t i = 0; i < n && g_file_include_filter.count < MAX_IGNORE_PATTERNS; i++) {
            PyObject *item = PyList_GET_ITEM(patterns_list, i);
            const char *pattern = PyUnicode_AsUTF8(item);
            if (!pattern) { PyErr_Clear(); continue; }
            char *dup = strdup(pattern);
            if (dup) g_file_include_filter.patterns[g_file_include_filter.count++] = dup;
        }
    }
    Py_RETURN_NONE;
}

PyObject *pyttd_set_exclude_patterns(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *func_list, *file_list;
    if (!PyArg_ParseTuple(args, "O!O!", &PyList_Type, &func_list, &PyList_Type, &file_list))
        return NULL;
    /* Clear existing exclude patterns */
    for (int i = 0; i < g_exclude_func_filter.count; i++) {
        free(g_exclude_func_filter.patterns[i]);
    }
    g_exclude_func_filter.count = 0;
    for (int i = 0; i < g_exclude_file_filter.count; i++) {
        free(g_exclude_file_filter.patterns[i]);
    }
    g_exclude_file_filter.count = 0;
    g_exclude_mode = 0;

    Py_ssize_t nf = PyList_GET_SIZE(func_list);
    for (Py_ssize_t i = 0; i < nf && g_exclude_func_filter.count < MAX_IGNORE_PATTERNS; i++) {
        const char *p = PyUnicode_AsUTF8(PyList_GET_ITEM(func_list, i));
        if (!p) { PyErr_Clear(); continue; }
#ifndef _WIN32
        if (!has_glob_chars(p)) {
            size_t len = strlen(p);
            char *wrapped = (char *)malloc(len + 3);
            if (wrapped) {
                snprintf(wrapped, len + 3, "*%s*", p);
                g_exclude_func_filter.patterns[g_exclude_func_filter.count++] = wrapped;
            }
        } else {
            char *dup = strdup(p);
            if (dup) g_exclude_func_filter.patterns[g_exclude_func_filter.count++] = dup;
        }
#else
        char *dup = strdup(p);
        if (dup) g_exclude_func_filter.patterns[g_exclude_func_filter.count++] = dup;
#endif
    }
    Py_ssize_t nfi = PyList_GET_SIZE(file_list);
    for (Py_ssize_t i = 0; i < nfi && g_exclude_file_filter.count < MAX_IGNORE_PATTERNS; i++) {
        const char *p = PyUnicode_AsUTF8(PyList_GET_ITEM(file_list, i));
        if (!p) { PyErr_Clear(); continue; }
        char *dup = strdup(p);
        if (dup) g_exclude_file_filter.patterns[g_exclude_file_filter.count++] = dup;
    }
    if (g_exclude_func_filter.count > 0 || g_exclude_file_filter.count > 0) {
        g_exclude_mode = 1;
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
