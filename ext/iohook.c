#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <string.h>
#include <stdatomic.h>
#include <datetime.h>
#include "iohook.h"
#include "recorder.h"

/* ---- Globals ---- */

static int g_io_replay_mode = 0;
static PyObject *g_io_replay_list = NULL;
static Py_ssize_t g_io_replay_cursor = 0;
static _Atomic uint64_t g_io_sequence = 0;
static PyObject *g_io_flush_callback = NULL;
static PyObject *g_io_replay_loader = NULL;

/* Saved originals — existing hooks */
static PyObject *g_orig_time_time = NULL;
static PyObject *g_orig_time_monotonic = NULL;
static PyObject *g_orig_time_perf_counter = NULL;
static PyObject *g_orig_random_random = NULL;
static PyObject *g_orig_random_randint = NULL;
static PyObject *g_orig_os_urandom = NULL;

/* Saved originals — new hooks */
static PyObject *g_orig_time_sleep = NULL;
static PyObject *g_orig_random_uniform = NULL;
static PyObject *g_orig_random_gauss = NULL;
static PyObject *g_orig_random_choice = NULL;
static PyObject *g_orig_random_sample = NULL;
static PyObject *g_orig_random_shuffle = NULL;
static PyObject *g_orig_uuid_uuid4 = NULL;
static PyObject *g_orig_uuid_uuid1 = NULL;

/* datetime subclass state */
static PyObject *g_orig_datetime_class = NULL;  /* original datetime.datetime */
static PyObject *g_orig_datetime_now = NULL;     /* bound method: original datetime.datetime.now */
static PyObject *g_orig_datetime_utcnow = NULL;  /* bound method: original datetime.datetime.utcnow */
static PyObject *g_hooked_datetime_class = NULL;  /* our subclass */

/* Cached pickle functions */
static PyObject *g_pickle_dumps = NULL;
static PyObject *g_pickle_loads = NULL;

/* ---- Pickle Initialization ---- */

static int init_pickle(void) {
    if (g_pickle_dumps) return 0;
    PyObject *mod = PyImport_ImportModule("pickle");
    if (!mod) { PyErr_Clear(); return -1; }
    g_pickle_dumps = PyObject_GetAttrString(mod, "dumps");
    g_pickle_loads = PyObject_GetAttrString(mod, "loads");
    Py_DECREF(mod);
    if (!g_pickle_dumps || !g_pickle_loads) {
        Py_XDECREF(g_pickle_dumps); g_pickle_dumps = NULL;
        Py_XDECREF(g_pickle_loads); g_pickle_loads = NULL;
        PyErr_Clear();
        return -1;
    }
    return 0;
}

/* ---- Serialization Helpers ---- */

/* Serialize a float as 8-byte IEEE 754 double (raw bytes) */
static PyObject *serialize_float(PyObject *value) {
    double d = PyFloat_AsDouble(value);
    if (d == -1.0 && PyErr_Occurred()) return NULL;
    return PyBytes_FromStringAndSize((const char *)&d, sizeof(double));
}

/* Serialize a C double directly */
static PyObject *serialize_double(double d) {
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

/* Serialize a Python object via pickle, then length-prefix the pickle bytes */
static PyObject *serialize_pickle(PyObject *value) {
    if (!g_pickle_dumps) {
        if (init_pickle() < 0) return NULL;
    }
    PyObject *pickled = PyObject_CallOneArg(g_pickle_dumps, value);
    if (!pickled) return NULL;
    PyObject *result = serialize_bytes(pickled);
    Py_DECREF(pickled);
    return result;
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

/* Deserialize length-prefixed bytes, then unpickle */
static PyObject *deserialize_pickle(PyObject *data) {
    PyObject *raw_bytes = deserialize_bytes(data);
    if (!raw_bytes) return NULL;
    if (!g_pickle_loads) {
        if (init_pickle() < 0) { Py_DECREF(raw_bytes); return NULL; }
    }
    PyObject *result = PyObject_CallOneArg(g_pickle_loads, raw_bytes);
    Py_DECREF(raw_bytes);
    return result;
}

/* Deserialize float → datetime.fromtimestamp or datetime.utcfromtimestamp */
static PyObject *deserialize_datetime(PyObject *data, int utc) {
    PyObject *ts_float = deserialize_float(data);
    if (!ts_float) return NULL;

    PyObject *cls = g_hooked_datetime_class ? g_hooked_datetime_class : g_orig_datetime_class;
    if (!cls) {
        /* No datetime class available — try importing */
        PyObject *dt_mod = PyImport_ImportModule("datetime");
        if (dt_mod) {
            cls = PyObject_GetAttrString(dt_mod, "datetime");
            Py_DECREF(dt_mod);
        }
        if (!cls) {
            PyErr_Clear();
            Py_DECREF(ts_float);
            Py_RETURN_NONE;
        }
        PyObject *result = PyObject_CallMethod(cls, utc ? "utcfromtimestamp" : "fromtimestamp",
                                               "O", ts_float);
        Py_DECREF(ts_float);
        Py_DECREF(cls);
        return result;
    }
    PyObject *result = PyObject_CallMethod(cls, utc ? "utcfromtimestamp" : "fromtimestamp",
                                           "O", ts_float);
    Py_DECREF(ts_float);
    return result;
}

/* Deserialize bytes → uuid.UUID(bytes=...) */
static PyObject *deserialize_uuid(PyObject *data) {
    PyObject *raw_bytes = deserialize_bytes(data);
    if (!raw_bytes) return NULL;

    PyObject *uuid_mod = PyImport_ImportModule("uuid");
    if (!uuid_mod) { Py_DECREF(raw_bytes); return NULL; }
    PyObject *UUID_cls = PyObject_GetAttrString(uuid_mod, "UUID");
    Py_DECREF(uuid_mod);
    if (!UUID_cls) { Py_DECREF(raw_bytes); return NULL; }

    PyObject *empty_args = PyTuple_New(0);
    if (!empty_args) { Py_DECREF(raw_bytes); Py_DECREF(UUID_cls); return NULL; }
    PyObject *kwargs = Py_BuildValue("{s:O}", "bytes", raw_bytes);
    Py_DECREF(raw_bytes);
    if (!kwargs) { Py_DECREF(empty_args); Py_DECREF(UUID_cls); return NULL; }

    PyObject *result = PyObject_Call(UUID_cls, empty_args, kwargs);
    Py_DECREF(empty_args);
    Py_DECREF(kwargs);
    Py_DECREF(UUID_cls);
    return result;
}

/* Deserialize a return value based on function name */
static PyObject *deserialize_return_value(const char *function_name, PyObject *data) {
    /* Existing float hooks */
    if (strcmp(function_name, "time.time") == 0 ||
        strcmp(function_name, "time.monotonic") == 0 ||
        strcmp(function_name, "time.perf_counter") == 0 ||
        strcmp(function_name, "random.random") == 0 ||
        strcmp(function_name, "random.uniform") == 0 ||
        strcmp(function_name, "random.gauss") == 0 ||
        strcmp(function_name, "time.sleep") == 0) {
        return deserialize_float(data);
    }
    /* Existing int hooks */
    if (strcmp(function_name, "random.randint") == 0) {
        return deserialize_int(data);
    }
    /* Existing bytes hooks */
    if (strcmp(function_name, "os.urandom") == 0) {
        return deserialize_bytes(data);
    }
    /* datetime hooks → reconstruct datetime objects */
    if (strcmp(function_name, "datetime.datetime.now") == 0) {
        return deserialize_datetime(data, 0);
    }
    if (strcmp(function_name, "datetime.datetime.utcnow") == 0) {
        return deserialize_datetime(data, 1);
    }
    /* uuid hooks → reconstruct UUID objects */
    if (strcmp(function_name, "uuid.uuid4") == 0 ||
        strcmp(function_name, "uuid.uuid1") == 0) {
        return deserialize_uuid(data);
    }
    /* pickle-based hooks */
    if (strcmp(function_name, "random.choice") == 0 ||
        strcmp(function_name, "random.sample") == 0 ||
        strcmp(function_name, "random.shuffle") == 0) {
        return deserialize_pickle(data);
    }
    /* Unknown function — return raw bytes */
    Py_INCREF(data);
    return data;
}

/* ---- IO Event Logging ---- */

/* Core logging — accepts pre-serialized bytes */
static void io_log_event_raw(const char *function_name, PyObject *serialized) {
    if (!g_io_flush_callback) return;

    /* sequence_no = g_sequence_counter - 1 (most recent frame event) */
    uint64_t cur_seq = recorder_get_sequence_counter();
    uint64_t io_seq = atomic_fetch_add_explicit(&g_io_sequence, 1, memory_order_relaxed);
    PyObject *dict = Py_BuildValue("{s:K,s:K,s:s,s:O}",
        "sequence_no", (unsigned long long)(cur_seq > 0 ? cur_seq - 1 : 0),
        "io_sequence", (unsigned long long)io_seq,
        "function_name", function_name,
        "return_value", serialized);
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

/* Convenience wrapper — auto-serializes based on Python type */
static void io_log_event(const char *function_name, PyObject *result) {
    PyObject *serialized = serialize_return_value(result);
    if (!serialized) {
        PyErr_WriteUnraisable(g_io_flush_callback);
        PyErr_Clear();
        return;
    }
    io_log_event_raw(function_name, serialized);
    Py_DECREF(serialized);
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

/* ---- Hook Functions — Existing ---- */

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

/* ---- Hook Functions — New: time.sleep ---- */

static PyObject *hooked_time_sleep(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) {
        /* Consume the replay event but skip sleeping */
        PyObject *dummy = io_replay_next("time.sleep");
        Py_XDECREF(dummy);
        Py_RETURN_NONE;
    }
    /* Get the duration argument for logging before calling original */
    PyObject *duration = NULL;
    if (PyTuple_GET_SIZE(args) > 0) {
        duration = PyTuple_GET_ITEM(args, 0);
    }
    PyObject *result = PyObject_Call(g_orig_time_sleep, args, kwargs);
    if (result == NULL) return NULL;
    /* Log the duration, not the return value (which is None) */
    if (duration && PyFloat_Check(duration)) {
        io_log_event("time.sleep", duration);
    } else if (duration) {
        /* duration could be int — convert to float for consistent serialization */
        PyObject *as_float = PyNumber_Float(duration);
        if (as_float) {
            io_log_event("time.sleep", as_float);
            Py_DECREF(as_float);
        } else {
            PyErr_Clear();
        }
    }
    return result;
}

/* ---- Hook Functions — New: random.uniform, random.gauss ---- */

static PyObject *hooked_random_uniform(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("random.uniform");
    PyObject *result = PyObject_Call(g_orig_random_uniform, args, kwargs);
    if (result == NULL) return NULL;
    io_log_event("random.uniform", result);
    return result;
}

static PyObject *hooked_random_gauss(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("random.gauss");
    PyObject *result = PyObject_Call(g_orig_random_gauss, args, kwargs);
    if (result == NULL) return NULL;
    io_log_event("random.gauss", result);
    return result;
}

/* ---- Hook Functions — New: random.choice, random.sample, random.shuffle ---- */

static PyObject *hooked_random_choice(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("random.choice");
    PyObject *result = PyObject_Call(g_orig_random_choice, args, kwargs);
    if (result == NULL) return NULL;
    PyObject *serialized = serialize_pickle(result);
    if (serialized) {
        io_log_event_raw("random.choice", serialized);
        Py_DECREF(serialized);
    } else {
        PyErr_Clear();
    }
    return result;
}

static PyObject *hooked_random_sample(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("random.sample");
    PyObject *result = PyObject_Call(g_orig_random_sample, args, kwargs);
    if (result == NULL) return NULL;
    PyObject *serialized = serialize_pickle(result);
    if (serialized) {
        io_log_event_raw("random.sample", serialized);
        Py_DECREF(serialized);
    } else {
        PyErr_Clear();
    }
    return result;
}

static PyObject *hooked_random_shuffle(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) {
        /* Get the list argument */
        PyObject *x = NULL;
        if (PyTuple_GET_SIZE(args) > 0) {
            x = PyTuple_GET_ITEM(args, 0);
        }
        /* Deserialize the recorded shuffled list */
        PyObject *shuffled = io_replay_next("random.shuffle");
        if (!shuffled || shuffled == Py_None) {
            Py_XDECREF(shuffled);
            Py_RETURN_NONE;
        }
        /* Copy elements into x in-place: x[:] = shuffled */
        if (x && PyList_Check(x) && PyList_Check(shuffled)) {
            if (PySequence_SetSlice(x, 0, PyList_GET_SIZE(x), shuffled) < 0) {
                PyErr_Clear();
            }
        }
        Py_DECREF(shuffled);
        Py_RETURN_NONE;
    }
    /* Call original shuffle (modifies list in-place, returns None) */
    PyObject *result = PyObject_Call(g_orig_random_shuffle, args, kwargs);
    if (result == NULL) return NULL;
    /* Serialize the shuffled list (first arg, modified in-place) */
    if (PyTuple_GET_SIZE(args) > 0) {
        PyObject *x = PyTuple_GET_ITEM(args, 0);
        PyObject *serialized = serialize_pickle(x);
        if (serialized) {
            io_log_event_raw("random.shuffle", serialized);
            Py_DECREF(serialized);
        } else {
            PyErr_Clear();
        }
    }
    return result;
}

/* ---- Hook Functions — New: uuid.uuid4, uuid.uuid1 ---- */

static PyObject *hooked_uuid_uuid4(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("uuid.uuid4");
    PyObject *result = PyObject_Call(g_orig_uuid_uuid4, args, kwargs);
    if (result == NULL) return NULL;
    /* Serialize UUID.bytes (16 bytes) */
    PyObject *uuid_bytes = PyObject_GetAttrString(result, "bytes");
    if (uuid_bytes) {
        PyObject *serialized = serialize_bytes(uuid_bytes);
        Py_DECREF(uuid_bytes);
        if (serialized) {
            io_log_event_raw("uuid.uuid4", serialized);
            Py_DECREF(serialized);
        } else {
            PyErr_Clear();
        }
    } else {
        PyErr_Clear();
    }
    return result;
}

static PyObject *hooked_uuid_uuid1(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("uuid.uuid1");
    PyObject *result = PyObject_Call(g_orig_uuid_uuid1, args, kwargs);
    if (result == NULL) return NULL;
    /* Serialize UUID.bytes (16 bytes) */
    PyObject *uuid_bytes = PyObject_GetAttrString(result, "bytes");
    if (uuid_bytes) {
        PyObject *serialized = serialize_bytes(uuid_bytes);
        Py_DECREF(uuid_bytes);
        if (serialized) {
            io_log_event_raw("uuid.uuid1", serialized);
            Py_DECREF(serialized);
        } else {
            PyErr_Clear();
        }
    } else {
        PyErr_Clear();
    }
    return result;
}

/* ---- Hook Functions — New: datetime.datetime.now, datetime.datetime.utcnow ---- */

/* Convert a datetime instance from the original class to our subclass instance.
 * This ensures isinstance(result, datetime.datetime) works after we replace
 * datetime.datetime with our subclass. Uses datetime.h C API macros. */
static PyObject *convert_datetime_to_subclass(PyObject *orig_result) {
    if (!g_hooked_datetime_class || !orig_result) return orig_result;
    /* Already our subclass — no conversion needed */
    if (PyObject_IsInstance(orig_result, g_hooked_datetime_class) == 1) return orig_result;

    if (!PyDateTimeAPI) {
        PyDateTime_IMPORT;
        if (!PyDateTimeAPI) return orig_result;
    }

    if (!PyDateTime_Check(orig_result)) return orig_result;

    int year = PyDateTime_GET_YEAR(orig_result);
    int month = PyDateTime_GET_MONTH(orig_result);
    int day = PyDateTime_GET_DAY(orig_result);
    int hour = PyDateTime_DATE_GET_HOUR(orig_result);
    int minute = PyDateTime_DATE_GET_MINUTE(orig_result);
    int second = PyDateTime_DATE_GET_SECOND(orig_result);
    int usec = PyDateTime_DATE_GET_MICROSECOND(orig_result);
    int fold = PyDateTime_DATE_GET_FOLD(orig_result);

    PyObject *tzinfo = PyObject_GetAttrString(orig_result, "tzinfo");
    if (!tzinfo) { PyErr_Clear(); tzinfo = Py_None; Py_INCREF(Py_None); }

    PyObject *pos_args = Py_BuildValue("(iiiiiii)", year, month, day,
                                       hour, minute, second, usec);
    if (!pos_args) { Py_DECREF(tzinfo); return orig_result; }

    PyObject *kw_args = NULL;
    if (tzinfo != Py_None || fold != 0) {
        kw_args = PyDict_New();
        if (!kw_args) { Py_DECREF(pos_args); Py_DECREF(tzinfo); return orig_result; }
        if (tzinfo != Py_None) PyDict_SetItemString(kw_args, "tzinfo", tzinfo);
        if (fold != 0) {
            PyObject *fold_obj = PyLong_FromLong(fold);
            if (fold_obj) {
                PyDict_SetItemString(kw_args, "fold", fold_obj);
                Py_DECREF(fold_obj);
            }
        }
    }
    Py_DECREF(tzinfo);

    PyObject *sub_result = PyObject_Call(g_hooked_datetime_class, pos_args, kw_args);
    Py_DECREF(pos_args);
    Py_XDECREF(kw_args);

    if (sub_result) {
        Py_DECREF(orig_result);
        return sub_result;
    }
    /* Fallback to original on conversion failure */
    PyErr_Clear();
    return orig_result;
}

/* The now() hook — installed as a classmethod on our datetime subclass.
 * When called through classmethod, args has cls prepended:
 *   SubClass.now()       → args=(cls,), kwargs=NULL
 *   SubClass.now(tz=utc) → args=(cls,), kwargs={tz: utc}
 *   SubClass.now(utc)    → args=(cls, utc), kwargs=NULL  */
static PyObject *hooked_datetime_now(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    if (g_io_replay_mode) return io_replay_next("datetime.datetime.now");

    /* Strip cls from args and forward to original now() */
    Py_ssize_t nargs = PyTuple_GET_SIZE(args);
    PyObject *fwd_args = PyTuple_GetSlice(args, 1, nargs);
    if (!fwd_args) return NULL;

    PyObject *result = PyObject_Call(g_orig_datetime_now, fwd_args, kwargs);
    Py_DECREF(fwd_args);
    if (result == NULL) return NULL;

    /* Log the timestamp */
    PyObject *ts = PyObject_CallMethod(result, "timestamp", NULL);
    if (ts) {
        PyObject *serialized = serialize_float(ts);
        Py_DECREF(ts);
        if (serialized) {
            io_log_event_raw("datetime.datetime.now", serialized);
            Py_DECREF(serialized);
        } else {
            PyErr_Clear();
        }
    } else {
        PyErr_Clear();
    }

    return convert_datetime_to_subclass(result);
}

static PyObject *hooked_datetime_utcnow(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    (void)args;
    (void)kwargs;
    if (g_io_replay_mode) return io_replay_next("datetime.datetime.utcnow");

    PyObject *result = PyObject_CallNoArgs(g_orig_datetime_utcnow);
    if (result == NULL) return NULL;

    /* utcnow() returns naive datetime in UTC. .timestamp() would assume local
     * timezone, giving wrong results. Instead: replace(tzinfo=UTC).timestamp()
     * to get a correct UTC epoch timestamp. */
    PyObject *dt_mod = PyImport_ImportModule("datetime");
    if (dt_mod) {
        PyObject *tz_cls = PyObject_GetAttrString(dt_mod, "timezone");
        Py_DECREF(dt_mod);
        if (tz_cls) {
            PyObject *utc = PyObject_GetAttrString(tz_cls, "utc");
            Py_DECREF(tz_cls);
            if (utc) {
                PyObject *kw = Py_BuildValue("{s:O}", "tzinfo", utc);
                Py_DECREF(utc);
                if (kw) {
                    PyObject *empty = PyTuple_New(0);
                    if (empty) {
                        PyObject *replace_method = PyObject_GetAttrString(result, "replace");
                        if (replace_method) {
                            PyObject *aware = PyObject_Call(replace_method, empty, kw);
                            Py_DECREF(replace_method);
                            if (aware) {
                                PyObject *ts = PyObject_CallMethod(aware, "timestamp", NULL);
                                Py_DECREF(aware);
                                if (ts) {
                                    PyObject *serialized = serialize_float(ts);
                                    Py_DECREF(ts);
                                    if (serialized) {
                                        io_log_event_raw("datetime.datetime.utcnow", serialized);
                                        Py_DECREF(serialized);
                                    } else {
                                        PyErr_Clear();
                                    }
                                } else {
                                    PyErr_Clear();
                                }
                            } else {
                                PyErr_Clear();
                            }
                        } else {
                            PyErr_Clear();
                        }
                        Py_DECREF(empty);
                    }
                    Py_DECREF(kw);
                }
            } else {
                PyErr_Clear();
            }
        } else {
            PyErr_Clear();
        }
    } else {
        PyErr_Clear();
    }

    return convert_datetime_to_subclass(result);
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

/* New hook PyMethodDefs */
static PyMethodDef hook_time_sleep_def = {
    "hooked_time_sleep", (PyCFunction)hooked_time_sleep,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_random_uniform_def = {
    "hooked_random_uniform", (PyCFunction)hooked_random_uniform,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_random_gauss_def = {
    "hooked_random_gauss", (PyCFunction)hooked_random_gauss,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_random_choice_def = {
    "hooked_random_choice", (PyCFunction)hooked_random_choice,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_random_sample_def = {
    "hooked_random_sample", (PyCFunction)hooked_random_sample,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_random_shuffle_def = {
    "hooked_random_shuffle", (PyCFunction)hooked_random_shuffle,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_uuid_uuid4_def = {
    "hooked_uuid_uuid4", (PyCFunction)hooked_uuid_uuid4,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_uuid_uuid1_def = {
    "hooked_uuid_uuid1", (PyCFunction)hooked_uuid_uuid1,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_datetime_now_def = {
    "hooked_datetime_now", (PyCFunction)hooked_datetime_now,
    METH_VARARGS | METH_KEYWORDS, NULL
};
static PyMethodDef hook_datetime_utcnow_def = {
    "hooked_datetime_utcnow", (PyCFunction)hooked_datetime_utcnow,
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

/* ---- datetime subclass install/restore ---- */

static int install_datetime_hooks(void) {
    /* Initialize the datetime C API (needed for PyDateTime_* macros) */
    if (!PyDateTimeAPI) {
        PyDateTime_IMPORT;
        if (!PyDateTimeAPI) {
            PyErr_Clear();
            return 0;
        }
    }

    PyObject *dt_mod = PyImport_ImportModule("datetime");
    if (!dt_mod) { PyErr_Clear(); return 0; }

    g_orig_datetime_class = PyObject_GetAttrString(dt_mod, "datetime");
    if (!g_orig_datetime_class) {
        PyErr_Clear();
        Py_DECREF(dt_mod);
        return 0;
    }

    /* Save references to original now/utcnow bound methods */
    g_orig_datetime_now = PyObject_GetAttrString(g_orig_datetime_class, "now");
    g_orig_datetime_utcnow = PyObject_GetAttrString(g_orig_datetime_class, "utcnow");
    if (!g_orig_datetime_now || !g_orig_datetime_utcnow) {
        PyErr_Clear();
        Py_XDECREF(g_orig_datetime_now); g_orig_datetime_now = NULL;
        Py_XDECREF(g_orig_datetime_utcnow); g_orig_datetime_utcnow = NULL;
        Py_CLEAR(g_orig_datetime_class);
        Py_DECREF(dt_mod);
        return 0;
    }

    /* Create PyCFunction objects for our hooks */
    PyObject *now_func = PyCFunction_New(&hook_datetime_now_def, NULL);
    PyObject *utcnow_func = PyCFunction_New(&hook_datetime_utcnow_def, NULL);
    if (!now_func || !utcnow_func) {
        Py_XDECREF(now_func);
        Py_XDECREF(utcnow_func);
        goto fail;
    }

    /* Wrap as classmethods using builtins.classmethod */
    PyObject *builtins = PyImport_ImportModule("builtins");
    if (!builtins) {
        Py_DECREF(now_func);
        Py_DECREF(utcnow_func);
        goto fail;
    }
    PyObject *classmethod_type = PyObject_GetAttrString(builtins, "classmethod");
    Py_DECREF(builtins);
    if (!classmethod_type) {
        Py_DECREF(now_func);
        Py_DECREF(utcnow_func);
        goto fail;
    }

    PyObject *cm_now = PyObject_CallOneArg(classmethod_type, now_func);
    PyObject *cm_utcnow = PyObject_CallOneArg(classmethod_type, utcnow_func);
    Py_DECREF(classmethod_type);
    Py_DECREF(now_func);
    Py_DECREF(utcnow_func);
    if (!cm_now || !cm_utcnow) {
        Py_XDECREF(cm_now);
        Py_XDECREF(cm_utcnow);
        goto fail;
    }

    /* Create namespace dict for the subclass */
    PyObject *ns = PyDict_New();
    if (!ns) { Py_DECREF(cm_now); Py_DECREF(cm_utcnow); goto fail; }
    PyDict_SetItemString(ns, "now", cm_now);
    PyDict_SetItemString(ns, "utcnow", cm_utcnow);
    Py_DECREF(cm_now);
    Py_DECREF(cm_utcnow);

    /* Create the subclass: type("datetime", (original_datetime,), ns) */
    PyObject *name = PyUnicode_FromString("datetime");
    PyObject *bases = PyTuple_Pack(1, g_orig_datetime_class);
    if (!name || !bases) {
        Py_XDECREF(name);
        Py_XDECREF(bases);
        Py_DECREF(ns);
        goto fail;
    }
    PyObject *type_args = PyTuple_Pack(3, name, bases, ns);
    Py_DECREF(name);
    Py_DECREF(bases);
    Py_DECREF(ns);
    if (!type_args) goto fail;

    g_hooked_datetime_class = PyObject_Call((PyObject *)&PyType_Type, type_args, NULL);
    Py_DECREF(type_args);
    if (!g_hooked_datetime_class) goto fail;

    /* Replace datetime.datetime with our subclass */
    if (PyObject_SetAttrString(dt_mod, "datetime", g_hooked_datetime_class) < 0) {
        Py_CLEAR(g_hooked_datetime_class);
        goto fail;
    }

    Py_DECREF(dt_mod);
    return 1;

fail:
    PyErr_Clear();
    Py_XDECREF(g_orig_datetime_now); g_orig_datetime_now = NULL;
    Py_XDECREF(g_orig_datetime_utcnow); g_orig_datetime_utcnow = NULL;
    Py_CLEAR(g_orig_datetime_class);
    Py_DECREF(dt_mod);
    return 0;
}

static void restore_datetime_hooks(void) {
    if (!g_orig_datetime_class) return;

    PyObject *dt_mod = PyImport_ImportModule("datetime");
    if (dt_mod) {
        PyObject_SetAttrString(dt_mod, "datetime", g_orig_datetime_class);
        if (PyErr_Occurred()) PyErr_Clear();
        Py_DECREF(dt_mod);
    } else {
        PyErr_Clear();
    }

    Py_XDECREF(g_orig_datetime_now); g_orig_datetime_now = NULL;
    Py_XDECREF(g_orig_datetime_utcnow); g_orig_datetime_utcnow = NULL;
    Py_CLEAR(g_orig_datetime_class);
    Py_CLEAR(g_hooked_datetime_class);
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
    /* Existing hooks */
    install_one_hook("time", "time", &hook_time_time_def, &g_orig_time_time);
    install_one_hook("time", "monotonic", &hook_time_monotonic_def, &g_orig_time_monotonic);
    install_one_hook("time", "perf_counter", &hook_time_perf_counter_def, &g_orig_time_perf_counter);
    install_one_hook("random", "random", &hook_random_random_def, &g_orig_random_random);
    install_one_hook("random", "randint", &hook_random_randint_def, &g_orig_random_randint);
    install_one_hook("os", "urandom", &hook_os_urandom_def, &g_orig_os_urandom);

    /* New hooks */
    install_one_hook("time", "sleep", &hook_time_sleep_def, &g_orig_time_sleep);
    install_one_hook("random", "uniform", &hook_random_uniform_def, &g_orig_random_uniform);
    install_one_hook("random", "gauss", &hook_random_gauss_def, &g_orig_random_gauss);
    install_one_hook("random", "choice", &hook_random_choice_def, &g_orig_random_choice);
    install_one_hook("random", "sample", &hook_random_sample_def, &g_orig_random_sample);
    install_one_hook("random", "shuffle", &hook_random_shuffle_def, &g_orig_random_shuffle);
    install_one_hook("uuid", "uuid4", &hook_uuid_uuid4_def, &g_orig_uuid_uuid4);
    install_one_hook("uuid", "uuid1", &hook_uuid_uuid1_def, &g_orig_uuid_uuid1);

    /* datetime hooks require subclass replacement */
    install_datetime_hooks();

    return 0;
}

void remove_io_hooks_internal(void) {
    /* Restore original functions — existing */
    restore_one_hook("time", "time", &g_orig_time_time);
    restore_one_hook("time", "monotonic", &g_orig_time_monotonic);
    restore_one_hook("time", "perf_counter", &g_orig_time_perf_counter);
    restore_one_hook("random", "random", &g_orig_random_random);
    restore_one_hook("random", "randint", &g_orig_random_randint);
    restore_one_hook("os", "urandom", &g_orig_os_urandom);

    /* Restore original functions — new */
    restore_one_hook("time", "sleep", &g_orig_time_sleep);
    restore_one_hook("random", "uniform", &g_orig_random_uniform);
    restore_one_hook("random", "gauss", &g_orig_random_gauss);
    restore_one_hook("random", "choice", &g_orig_random_choice);
    restore_one_hook("random", "sample", &g_orig_random_sample);
    restore_one_hook("random", "shuffle", &g_orig_random_shuffle);
    restore_one_hook("uuid", "uuid4", &g_orig_uuid_uuid4);
    restore_one_hook("uuid", "uuid1", &g_orig_uuid_uuid1);

    /* Restore datetime class */
    restore_datetime_hooks();

    /* Clean up pickle cache */
    Py_XDECREF(g_pickle_dumps); g_pickle_dumps = NULL;
    Py_XDECREF(g_pickle_loads); g_pickle_loads = NULL;

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
