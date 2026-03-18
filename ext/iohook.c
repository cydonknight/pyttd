#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <string.h>
#include <stdatomic.h>
#include "iohook.h"
#include "recorder.h"

/* ---- Globals ---- */

static int g_io_replay_mode = 0;
static PyObject *g_io_replay_list = NULL;
static Py_ssize_t g_io_replay_cursor = 0;
static _Atomic uint64_t g_io_sequence = 0;
static PyObject *g_io_flush_callback = NULL;
static PyObject *g_io_replay_loader = NULL;

/* Saved originals */
static PyObject *g_orig_time_time = NULL;
static PyObject *g_orig_time_monotonic = NULL;
static PyObject *g_orig_time_perf_counter = NULL;
static PyObject *g_orig_random_random = NULL;
static PyObject *g_orig_random_randint = NULL;
static PyObject *g_orig_os_urandom = NULL;

/* ---- Serialization Helpers ---- */

/* Serialize a float as 8-byte IEEE 754 double (raw bytes) */
static PyObject *serialize_float(PyObject *value) {
    double d = PyFloat_AsDouble(value);
    if (d == -1.0 && PyErr_Occurred()) return NULL;
    return PyBytes_FromStringAndSize((const char *)&d, sizeof(double));
}

/* Serialize an int as length-prefixed bytes */
static PyObject *serialize_int(PyObject *value) {
    /* Use Python's int.to_bytes with big-endian, signed */
    size_t nbits;
    int overflow;
    long long val = PyLong_AsLongLongAndOverflow(value, &overflow);
    if (overflow || (val == -1 && PyErr_Occurred())) {
        /* Big int — use Python's to_bytes method */
        if (PyErr_Occurred()) PyErr_Clear();
        PyObject *bit_length = PyObject_CallMethod(value, "bit_length", NULL);
        if (!bit_length) return NULL;
        long bits = PyLong_AsLong(bit_length);
        Py_DECREF(bit_length);
        if (bits == -1 && PyErr_Occurred()) return NULL;
        nbits = (size_t)bits;
    } else {
        if (val < 0) val = -val - 1;
        nbits = 0;
        long long tmp = val;
        while (tmp > 0) { nbits++; tmp >>= 1; }
    }
    /* Need (nbits + 8) // 8 bytes for signed representation */
    Py_ssize_t byte_len = (Py_ssize_t)((nbits + 8) / 8);
    if (byte_len < 1) byte_len = 1;

    PyObject *length_arg = PyLong_FromSsize_t(byte_len);
    if (!length_arg) return NULL;
    PyObject *byteorder = PyUnicode_FromString("big");
    if (!byteorder) { Py_DECREF(length_arg); return NULL; }

    /* signed is keyword-only in int.to_bytes() */
    PyObject *to_bytes_func = PyObject_GetAttrString(value, "to_bytes");
    if (!to_bytes_func) { Py_DECREF(length_arg); Py_DECREF(byteorder); return NULL; }
    PyObject *pos_args = PyTuple_Pack(2, length_arg, byteorder);
    Py_DECREF(length_arg);
    Py_DECREF(byteorder);
    if (!pos_args) { Py_DECREF(to_bytes_func); return NULL; }
    PyObject *kw = Py_BuildValue("{s:O}", "signed", Py_True);
    if (!kw) { Py_DECREF(pos_args); Py_DECREF(to_bytes_func); return NULL; }
    PyObject *bytes_val = PyObject_Call(to_bytes_func, pos_args, kw);
    Py_DECREF(to_bytes_func);
    Py_DECREF(pos_args);
    Py_DECREF(kw);
    if (!bytes_val) return NULL;

    /* Prefix with 4-byte length (native byte order — both sides run on same machine) */
    Py_ssize_t data_len = PyBytes_GET_SIZE(bytes_val);
    if (data_len > (Py_ssize_t)UINT32_MAX) {
        Py_DECREF(bytes_val);
        PyErr_SetString(PyExc_OverflowError, "Integer too large for I/O hook serialization");
        return NULL;
    }
    uint32_t net_len = (uint32_t)data_len;
    PyObject *prefix = PyBytes_FromStringAndSize((const char *)&net_len, 4);
    if (!prefix) { Py_DECREF(bytes_val); return NULL; }

    PyBytes_Concat(&prefix, bytes_val);
    Py_DECREF(bytes_val);
    return prefix;  /* may be NULL if concat failed */
}

/* Serialize bytes as length-prefixed binary */
static PyObject *serialize_bytes(PyObject *value) {
    Py_ssize_t data_len = PyBytes_GET_SIZE(value);
    if (data_len > (Py_ssize_t)UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError, "Bytes too large for I/O hook serialization");
        return NULL;
    }
    uint32_t len_prefix = (uint32_t)data_len;
    PyObject *prefix = PyBytes_FromStringAndSize((const char *)&len_prefix, 4);
    if (!prefix) return NULL;
    PyBytes_Concat(&prefix, value);
    return prefix;
}

/* Serialize any return value type-specifically */
static PyObject *serialize_return_value(PyObject *value) {
    if (PyFloat_Check(value)) {
        return serialize_float(value);
    } else if (PyLong_Check(value)) {
        return serialize_int(value);
    } else if (PyBytes_Check(value)) {
        return serialize_bytes(value);
    } else {
        /* Fallback: use repr as bytes */
        PyObject *repr = PyObject_Repr(value);
        if (!repr) return NULL;
        PyObject *encoded = PyUnicode_AsUTF8String(repr);
        Py_DECREF(repr);
        if (!encoded) return NULL;
        PyObject *result = serialize_bytes(encoded);
        Py_DECREF(encoded);
        return result;
    }
}

/* ---- Deserialization Helpers ---- */

static PyObject *deserialize_float(PyObject *data) {
    if (!PyBytes_Check(data) || PyBytes_GET_SIZE(data) != sizeof(double)) {
        PyErr_SetString(PyExc_ValueError, "Invalid float serialization");
        return NULL;
    }
    double d;
    memcpy(&d, PyBytes_AS_STRING(data), sizeof(double));
    return PyFloat_FromDouble(d);
}

static PyObject *deserialize_int(PyObject *data) {
    if (!PyBytes_Check(data) || PyBytes_GET_SIZE(data) < 4) {
        PyErr_SetString(PyExc_ValueError, "Invalid int serialization");
        return NULL;
    }
    const char *buf = PyBytes_AS_STRING(data);
    uint32_t len_prefix;
    memcpy(&len_prefix, buf, 4);
    Py_ssize_t byte_len = (Py_ssize_t)len_prefix;
    if (PyBytes_GET_SIZE(data) < 4 + byte_len) {
        PyErr_SetString(PyExc_ValueError, "Int data truncated");
        return NULL;
    }
    PyObject *int_bytes = PyBytes_FromStringAndSize(buf + 4, byte_len);
    if (!int_bytes) return NULL;
    PyObject *byteorder = PyUnicode_FromString("big");
    if (!byteorder) { Py_DECREF(int_bytes); return NULL; }
    /* signed is keyword-only in int.from_bytes() */
    PyObject *from_bytes = PyObject_GetAttrString((PyObject *)&PyLong_Type, "from_bytes");
    if (!from_bytes) { Py_DECREF(int_bytes); Py_DECREF(byteorder); return NULL; }
    PyObject *pos_args = PyTuple_Pack(2, int_bytes, byteorder);
    Py_DECREF(int_bytes);
    Py_DECREF(byteorder);
    if (!pos_args) { Py_DECREF(from_bytes); return NULL; }
    PyObject *kw = Py_BuildValue("{s:O}", "signed", Py_True);
    if (!kw) { Py_DECREF(pos_args); Py_DECREF(from_bytes); return NULL; }
    PyObject *result = PyObject_Call(from_bytes, pos_args, kw);
    Py_DECREF(from_bytes);
    Py_DECREF(pos_args);
    Py_DECREF(kw);
    return result;
}

static PyObject *deserialize_bytes(PyObject *data) {
    if (!PyBytes_Check(data) || PyBytes_GET_SIZE(data) < 4) {
        PyErr_SetString(PyExc_ValueError, "Invalid bytes serialization");
        return NULL;
    }
    const char *buf = PyBytes_AS_STRING(data);
    uint32_t len_prefix;
    memcpy(&len_prefix, buf, 4);
    Py_ssize_t byte_len = (Py_ssize_t)len_prefix;
    if (PyBytes_GET_SIZE(data) < 4 + byte_len) {
        PyErr_SetString(PyExc_ValueError, "Bytes data truncated");
        return NULL;
    }
    return PyBytes_FromStringAndSize(buf + 4, byte_len);
}

/* Deserialize a return value based on function name */
static PyObject *deserialize_return_value(const char *function_name, PyObject *data) {
    if (strcmp(function_name, "time.time") == 0 ||
        strcmp(function_name, "time.monotonic") == 0 ||
        strcmp(function_name, "time.perf_counter") == 0 ||
        strcmp(function_name, "random.random") == 0) {
        return deserialize_float(data);
    } else if (strcmp(function_name, "random.randint") == 0) {
        return deserialize_int(data);
    } else if (strcmp(function_name, "os.urandom") == 0) {
        return deserialize_bytes(data);
    }
    /* Unknown function — return raw bytes */
    Py_INCREF(data);
    return data;
}

/* ---- IO Event Logging ---- */

static void io_log_event(const char *function_name, PyObject *result) {
    if (!g_io_flush_callback) return;

    PyObject *serialized = serialize_return_value(result);
    if (!serialized) {
        PyErr_WriteUnraisable(g_io_flush_callback);
        PyErr_Clear();
        return;
    }

    /* sequence_no = g_sequence_counter - 1 (most recent frame event) */
    uint64_t cur_seq = recorder_get_sequence_counter();
    uint64_t io_seq = atomic_fetch_add_explicit(&g_io_sequence, 1, memory_order_relaxed);
    PyObject *dict = Py_BuildValue("{s:K,s:K,s:s,s:O}",
        "sequence_no", (unsigned long long)(cur_seq > 0 ? cur_seq - 1 : 0),
        "io_sequence", (unsigned long long)io_seq,
        "function_name", function_name,
        "return_value", serialized);
    Py_DECREF(serialized);
    if (!dict) {
        PyErr_WriteUnraisable(g_io_flush_callback);
        PyErr_Clear();
        return;
    }

    /* Suppress recording of the callback's Python calls (Peewee DB insert etc.)
     * — same guard used by serialize_one_local to suppress repr() recording. */
    g_inside_repr = 1;
    PyObject *cb_result = PyObject_CallOneArg(g_io_flush_callback, dict);
    g_inside_repr = 0;
    Py_DECREF(dict);
    if (!cb_result) {
        PyErr_WriteUnraisable(g_io_flush_callback);
        PyErr_Clear();
    } else {
        Py_DECREF(cb_result);
    }
}

/* ---- IO Replay ---- */

static PyObject *io_replay_next(const char *function_name) {
    if (!g_io_replay_list || !PyList_Check(g_io_replay_list)) {
        PyErr_SetString(PyExc_RuntimeError, "IO replay list not available");
        return NULL;
    }

    if (g_io_replay_cursor >= PyList_GET_SIZE(g_io_replay_list)) {
        /* More I/O calls than recorded — fall back warning */
        PyErr_WarnFormat(PyExc_RuntimeWarning, 1,
            "IO replay: cursor exhausted for %s, returning None", function_name);
        if (PyErr_Occurred()) PyErr_Clear();
        Py_RETURN_NONE;
    }

    PyObject *entry = PyList_GET_ITEM(g_io_replay_list, g_io_replay_cursor);
    g_io_replay_cursor++;

    /* Get function_name from entry and verify match */
    PyObject *fn_obj = PyDict_GetItemString(entry, "function_name");
    if (fn_obj) {
        const char *fn_str = PyUnicode_AsUTF8(fn_obj);
        if (fn_str && strcmp(fn_str, function_name) != 0) {
            PyErr_WarnFormat(PyExc_RuntimeWarning, 1,
                "IO replay: expected %s but got %s (non-determinism detected)",
                function_name, fn_str);
            if (PyErr_Occurred()) PyErr_Clear();
        }
    }

    /* Get return_value and deserialize */
    PyObject *rv_obj = PyDict_GetItemString(entry, "return_value");
    if (!rv_obj) {
        Py_RETURN_NONE;
    }

    /* rv_obj may be bytes (from Peewee BlobField) or memoryview */
    if (!PyBytes_Check(rv_obj)) {
        PyObject *bytes_val = PyBytes_FromObject(rv_obj);
        if (!bytes_val) {
            PyErr_Clear();
            Py_RETURN_NONE;
        }
        PyObject *result = deserialize_return_value(function_name, bytes_val);
        Py_DECREF(bytes_val);
        return result;
    }
    return deserialize_return_value(function_name, rv_obj);
}

/* ---- Hook Functions ---- */

static PyObject *hooked_time_time(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("time.time");
    PyObject *result = PyObject_Call(g_orig_time_time, args, kwargs);
    if (result == NULL) return NULL;
    io_log_event("time.time", result);
    return result;
}

static PyObject *hooked_time_monotonic(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("time.monotonic");
    PyObject *result = PyObject_Call(g_orig_time_monotonic, args, kwargs);
    if (result == NULL) return NULL;
    io_log_event("time.monotonic", result);
    return result;
}

static PyObject *hooked_time_perf_counter(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("time.perf_counter");
    PyObject *result = PyObject_Call(g_orig_time_perf_counter, args, kwargs);
    if (result == NULL) return NULL;
    io_log_event("time.perf_counter", result);
    return result;
}

static PyObject *hooked_random_random(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("random.random");
    PyObject *result = PyObject_Call(g_orig_random_random, args, kwargs);
    if (result == NULL) return NULL;
    io_log_event("random.random", result);
    return result;
}

static PyObject *hooked_random_randint(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("random.randint");
    PyObject *result = PyObject_Call(g_orig_random_randint, args, kwargs);
    if (result == NULL) return NULL;
    io_log_event("random.randint", result);
    return result;
}

static PyObject *hooked_os_urandom(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("os.urandom");
    PyObject *result = PyObject_Call(g_orig_os_urandom, args, kwargs);
    if (result == NULL) return NULL;
    io_log_event("os.urandom", result);
    return result;
}

/* ---- PyMethodDef for hook functions ---- */

static PyMethodDef hook_time_time_def = {
    "hooked_time_time", (PyCFunction)hooked_time_time,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_time_monotonic_def = {
    "hooked_time_monotonic", (PyCFunction)hooked_time_monotonic,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_time_perf_counter_def = {
    "hooked_time_perf_counter", (PyCFunction)hooked_time_perf_counter,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_random_random_def = {
    "hooked_random_random", (PyCFunction)hooked_random_random,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_random_randint_def = {
    "hooked_random_randint", (PyCFunction)hooked_random_randint,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_os_urandom_def = {
    "hooked_os_urandom", (PyCFunction)hooked_os_urandom,
    METH_VARARGS | METH_KEYWORDS, NULL
};

/* ---- Helper: install one hook ---- */

static int install_one_hook(const char *module_name, const char *attr_name,
                             PyMethodDef *hook_def, PyObject **orig_save) {
    PyObject *mod = PyImport_ImportModule(module_name);
    if (!mod) {
        PyErr_Clear();  /* skip gracefully if module unavailable */
        return 0;
    }

    PyObject *orig = PyObject_GetAttrString(mod, attr_name);
    if (!orig) {
        PyErr_Clear();
        Py_DECREF(mod);
        return 0;
    }

    PyObject *hook_func = PyCFunction_New(hook_def, NULL);
    if (!hook_func) {
        Py_DECREF(orig);
        Py_DECREF(mod);
        return -1;
    }

    if (PyObject_SetAttrString(mod, attr_name, hook_func) < 0) {
        PyErr_Clear();
        Py_DECREF(hook_func);
        Py_DECREF(orig);
        Py_DECREF(mod);
        return -1;
    }

    *orig_save = orig;  /* transfer ownership */
    Py_DECREF(hook_func);
    Py_DECREF(mod);
    return 1;
}

/* ---- Helper: restore one hook ---- */

static void restore_one_hook(const char *module_name, const char *attr_name,
                              PyObject **orig_save) {
    if (*orig_save == NULL) return;

    PyObject *mod = PyImport_ImportModule(module_name);
    if (mod) {
        PyObject_SetAttrString(mod, attr_name, *orig_save);
        if (PyErr_Occurred()) PyErr_Clear();
        Py_DECREF(mod);
    } else {
        PyErr_Clear();
    }
    Py_DECREF(*orig_save);
    *orig_save = NULL;
}

/* ---- Public API ---- */

int install_io_hooks_internal(PyObject *io_flush_callback, PyObject *io_replay_loader) {
    if (!io_flush_callback) return 0;

    g_io_flush_callback = io_flush_callback;
    Py_INCREF(g_io_flush_callback);

    if (io_replay_loader && io_replay_loader != Py_None) {
        g_io_replay_loader = io_replay_loader;
        Py_INCREF(g_io_replay_loader);
    }

    atomic_store_explicit(&g_io_sequence, 0, memory_order_relaxed);
    g_io_replay_mode = 0;
    g_io_replay_cursor = 0;
    Py_XDECREF(g_io_replay_list);
    g_io_replay_list = NULL;

    /* Install hooks — failures are non-fatal */
    install_one_hook("time", "time", &hook_time_time_def, &g_orig_time_time);
    install_one_hook("time", "monotonic", &hook_time_monotonic_def, &g_orig_time_monotonic);
    install_one_hook("time", "perf_counter", &hook_time_perf_counter_def, &g_orig_time_perf_counter);
    install_one_hook("random", "random", &hook_random_random_def, &g_orig_random_random);
    install_one_hook("random", "randint", &hook_random_randint_def, &g_orig_random_randint);
    install_one_hook("os", "urandom", &hook_os_urandom_def, &g_orig_os_urandom);

    return 0;
}

void remove_io_hooks_internal(void) {
    /* Restore original functions */
    restore_one_hook("time", "time", &g_orig_time_time);
    restore_one_hook("time", "monotonic", &g_orig_time_monotonic);
    restore_one_hook("time", "perf_counter", &g_orig_time_perf_counter);
    restore_one_hook("random", "random", &g_orig_random_random);
    restore_one_hook("random", "randint", &g_orig_random_randint);
    restore_one_hook("os", "urandom", &g_orig_os_urandom);

    Py_XDECREF(g_io_flush_callback);
    g_io_flush_callback = NULL;
    Py_XDECREF(g_io_replay_loader);
    g_io_replay_loader = NULL;
    Py_XDECREF(g_io_replay_list);
    g_io_replay_list = NULL;

    g_io_sequence = 0;
    g_io_replay_mode = 0;
    g_io_replay_cursor = 0;
}

void iohook_enter_replay_mode(uint64_t checkpoint_seq) {
    if (!g_io_replay_loader) return;

    g_io_replay_mode = 1;
    g_io_replay_cursor = 0;

    /* Call the replay loader with checkpoint_seq */
    PyObject *result = PyObject_CallFunction(g_io_replay_loader, "K",
                                             (unsigned long long)checkpoint_seq);
    if (!result) {
        PyErr_WriteUnraisable(g_io_replay_loader);
        PyErr_Clear();
        g_io_replay_mode = 0;
        return;
    }

    Py_XDECREF(g_io_replay_list);
    g_io_replay_list = result;  /* transfer ownership */
}

void iohook_reset_child_state(void) {
    g_io_replay_mode = 0;
    g_io_replay_cursor = 0;
    Py_XDECREF(g_io_replay_list);
    g_io_replay_list = NULL;
}
