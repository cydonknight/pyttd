#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <frameobject.h>
#include "platform.h"
#include "checkpoint.h"
#include "checkpoint_store.h"
#include "recorder.h"
#include "ringbuf.h"
#include "iohook.h"
#include "binlog.h"

#ifdef PYTTD_HAS_FORK

#include <unistd.h>
#include <sys/wait.h>
#include <signal.h>
#include <errno.h>
#include <string.h>
#include <stdio.h>
#include <arpa/inet.h>
#include <stdatomic.h>
#include <time.h>

/* External flush thread sync from recorder.c */
extern pthread_mutex_t g_flush_mutex;
extern pthread_cond_t g_flush_cond;

/* Pre-fork synchronization condvars (defined in recorder.c) */
extern pthread_cond_t g_pause_ack_cv;
extern pthread_cond_t g_resume_cv;

/* RESUME_LIVE support: trace function, callback, socket FD (defined in recorder.c) */
extern int pyttd_trace_func(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg);
extern PyObject *g_resume_live_callback;
extern int g_server_socket_fd;
extern _Atomic int g_flush_stop;
extern pthread_t g_flush_thread;
extern _Atomic int g_pause_requested;
extern _Atomic int g_pause_acked;

/* ---- Pipe I/O Helpers ---- */

static ssize_t write_all(int fd, const void *buf, size_t len) {
    size_t written = 0;
    while (written < len) {
        ssize_t n = write(fd, (const char *)buf + written, len - written);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        written += n;
    }
    return (ssize_t)written;
}

static ssize_t read_all(int fd, void *buf, size_t len) {
    size_t total = 0;
    while (total < len) {
        ssize_t n = read(fd, (char *)buf + total, len - total);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            return n;  /* 0 = EOF, -1 = error */
        }
        total += n;
    }
    return (ssize_t)total;
}

/* ---- State Serialization ---- */

#define RESULT_BUF_SIZE (MAX_LOCALS_JSON_SIZE + 1024)

int serialize_target_state(int result_fd, int event_type, PyObject *trace_arg) {
    PyFrameObject *frame = PyThreadState_GetFrame(PyThreadState_Get());
    if (!frame) {
        serialize_error_result(result_fd, "no_frame", recorder_get_sequence_counter());
        return -1;
    }
    PyCodeObject *code = PyFrame_GetCode(frame);
    if (!code) {
        Py_DECREF(frame);
        serialize_error_result(result_fd, "no_code", recorder_get_sequence_counter());
        return -1;
    }
    int line_no = PyFrame_GetLineNumber(frame);
    const char *filename = PyUnicode_AsUTF8(code->co_filename);
    const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

    if (!filename) { PyErr_Clear(); filename = "<unknown>"; }
    if (!funcname) { PyErr_Clear(); funcname = "<unknown>"; }

    /* Event-type-specific extra locals */
    PyObject *extra_key = NULL, *extra_val = NULL;
    if (event_type == PyTrace_RETURN && trace_arg != NULL) {
        extra_key = PyUnicode_FromString("__return__");
        extra_val = trace_arg;
    } else if (event_type == PyTrace_EXCEPTION && trace_arg != NULL) {
        extra_key = PyUnicode_FromString("__exception__");
        if (PyTuple_Check(trace_arg) && PyTuple_GET_SIZE(trace_arg) >= 2)
            extra_val = PyTuple_GET_ITEM(trace_arg, 1);
    }

    const char *locals_json = recorder_serialize_locals(
        (PyObject *)frame, g_locals_buf, sizeof(g_locals_buf),
        extra_key, extra_val);
    Py_XDECREF(extra_key);

    char escaped_filename[512], escaped_funcname[512];
    if (recorder_json_escape_string(filename, escaped_filename, sizeof(escaped_filename)) < 0) {
        escaped_filename[0] = '\0';
    }
    if (recorder_json_escape_string(funcname, escaped_funcname, sizeof(escaped_funcname)) < 0) {
        escaped_funcname[0] = '\0';
    }

    char *result_buf = (char *)malloc(RESULT_BUF_SIZE);
    if (!result_buf) {
        Py_DECREF(code);
        Py_DECREF(frame);
        serialize_error_result(result_fd, "out_of_memory", recorder_get_sequence_counter());
        return -1;
    }

    int len = snprintf(result_buf, RESULT_BUF_SIZE,
        "{\"status\": \"ok\", \"seq\": %llu, \"file\": \"%s\", \"line\": %d, "
        "\"function_name\": \"%s\", \"call_depth\": %d, \"locals\": %s}",
        (unsigned long long)recorder_get_sequence_counter(),
        escaped_filename, line_no,
        escaped_funcname, recorder_get_call_depth(),
        locals_json ? locals_json : "{}");

    Py_DECREF(code);
    Py_DECREF(frame);

    if (len < 0 || (size_t)len >= RESULT_BUF_SIZE) {
        free(result_buf);
        const char *err = "{\"status\": \"error\", \"error\": \"result_too_large\"}";
        uint32_t err_len = htonl((uint32_t)strlen(err));
        write_all(result_fd, &err_len, 4);
        write_all(result_fd, err, strlen(err));
        return -1;
    }

    uint32_t net_len = htonl((uint32_t)len);
    write_all(result_fd, &net_len, 4);
    write_all(result_fd, result_buf, len);
    free(result_buf);
    return 0;
}

void serialize_error_result(int result_fd, const char *error_code, uint64_t last_seq) {
    char buf[256];
    int len = snprintf(buf, sizeof(buf),
        "{\"status\": \"error\", \"error\": \"%s\", \"last_seq\": %llu}",
        error_code, (unsigned long long)last_seq);
    if (len < 0 || (size_t)len >= sizeof(buf)) len = (int)sizeof(buf) - 1;
    uint32_t net_len = htonl((uint32_t)len);
    write_all(result_fd, &net_len, 4);
    write_all(result_fd, buf, len);
}

/* ---- RESUME_LIVE: Child Goes Live ---- */

void checkpoint_child_go_live(int result_fd, int cmd_fd) {
    /* Called from fast-forward hooks when target is reached in RESUME_LIVE mode.
     * Re-initializes the child as a live recording process. */

    /* 1. Clear fast-forward state */
    g_fast_forward_live = 0;
    recorder_set_fast_forward(0, 0);  /* clears g_fast_forward and g_fast_forward_target */

    /* 2. Generate new run_id */
    PyObject *uuid_mod = PyImport_ImportModule("uuid");
    if (!uuid_mod) { PyErr_Clear(); _exit(1); }
    PyObject *uuid4 = PyObject_CallMethod(uuid_mod, "uuid4", NULL);
    if (!uuid4) { PyErr_Clear(); Py_DECREF(uuid_mod); _exit(1); }
    PyObject *hex_attr = PyObject_GetAttrString(uuid4, "hex");
    if (!hex_attr) { PyErr_Clear(); Py_DECREF(uuid4); Py_DECREF(uuid_mod); _exit(1); }
    const char *new_run_id = PyUnicode_AsUTF8(hex_attr);

    /* 3. Reinitialize ring buffer */
    ringbuf_system_init(PYTTD_DEFAULT_CAPACITY);
    ringbuf_get_or_create(PyThread_get_thread_ident());

    /* 4. Open new binlog for the branch */
    binlog_open(g_recorder_db_path, new_run_id);

    /* 5. Re-enable recording */
    atomic_store_explicit(&g_recording, 1, memory_order_release);
    atomic_store_explicit(&g_stop_requested, 0, memory_order_relaxed);

    /* 6. Reinstall trace function */
    PyEval_SetTrace((Py_tracefunc)pyttd_trace_func, Py_None);

    /* 7. Reset I/O hooks to recording mode */
    iohook_reset_child_state();

    /* 8. Re-register signal handlers (were SIG_IGN in checkpoint_child_init) */
    signal(SIGINT, SIG_DFL);
    signal(SIGTERM, SIG_DFL);
    signal(SIGPIPE, SIG_IGN);

    /* 9. Restart flush thread */
    atomic_store_explicit(&g_flush_stop, 0, memory_order_relaxed);
    g_flush_thread_created = 0;
    if (pthread_create(&g_flush_thread, NULL, flush_thread_func, NULL) == 0) {
        g_flush_thread_created = 1;
    }

    /* 10. Reset checkpoint state */
    atomic_store_explicit(&g_last_checkpoint_seq,
        atomic_load_explicit(&g_sequence_counter, memory_order_relaxed),
        memory_order_relaxed);

    /* 11. Reset user pause state */
    atomic_store_explicit(&g_user_pause_requested, 0, memory_order_relaxed);
    atomic_store_explicit(&g_user_paused, 0, memory_order_relaxed);
    atomic_store_explicit(&g_user_pause_thread_count, 0, memory_order_relaxed);

    /* 12. Send handoff result to parent via result pipe */
    char result_json[512];
    uint64_t cur_seq = atomic_load_explicit(&g_sequence_counter, memory_order_relaxed);
    snprintf(result_json, sizeof(result_json),
        "{\"status\":\"live\",\"new_run_id\":\"%s\",\"seq\":%llu}",
        new_run_id, (unsigned long long)cur_seq);

    uint32_t net_len = htonl((uint32_t)strlen(result_json));
    write_all(result_fd, &net_len, 4);
    write_all(result_fd, result_json, strlen(result_json));

    /* 13. Close pipe FDs — parent reads the result and shuts down */
    close(result_fd);
    close(cmd_fd);
    g_cmd_fd = -1;
    g_result_fd = -1;

    /* 14. Call Python bootstrap callback to start the RPC event loop
     * in a background thread (the main thread continues the user script) */
    if (g_resume_live_callback) {
        extern int g_server_socket_fd;
        PyObject *result = PyObject_CallFunction(g_resume_live_callback,
            "ssi", new_run_id, g_recorder_db_path, g_server_socket_fd);
        if (!result) {
            PyErr_WriteUnraisable(g_resume_live_callback);
            PyErr_Clear();
        }
        Py_XDECREF(result);
    }

    /* 15. Cleanup Python refs */
    Py_DECREF(hex_attr);
    Py_DECREF(uuid4);
    Py_DECREF(uuid_mod);

    /* Return to caller (fast-forward hook).
     * g_fast_forward_live is now 0, so the eval hook proceeds normally.
     * The child is now the live process. */
}

/* ---- Child Command Loop ---- */

int checkpoint_wait_for_command(int cmd_fd) {
    PyThreadState *tstate = PyEval_SaveThread();  /* release GIL */
    uint8_t cmd_buf[9];
    ssize_t n = read_all(cmd_fd, cmd_buf, 9);
    if (n <= 0) _exit(0);

    uint8_t opcode = cmd_buf[0];
    uint64_t payload;
    memcpy(&payload, cmd_buf + 1, sizeof(uint64_t));
    payload = pyttd_be64toh(payload);

    PyEval_RestoreThread(tstate);  /* re-acquire GIL */

    if (opcode == 0xFF) _exit(0);  /* DIE */

    if (opcode == 0x01) {  /* RESUME(target) */
        if (payload <= recorder_get_sequence_counter()) {
            serialize_error_result(g_result_fd, "already_past_target",
                                   recorder_get_sequence_counter());
            return checkpoint_wait_for_command(cmd_fd);
        }
        recorder_set_fast_forward(1, payload);
        return 0;
    }

    if (opcode == 0x02) {  /* STEP(delta) */
        if (payload == 0) {
            serialize_target_state(g_result_fd, -1, NULL);
            return checkpoint_wait_for_command(cmd_fd);
        }
        recorder_set_fast_forward(1, recorder_get_sequence_counter() + payload);
        return 0;
    }

    if (opcode == 0x03) {  /* RESUME_LIVE(target) */
        if (payload <= recorder_get_sequence_counter()) {
            serialize_error_result(g_result_fd, "already_past_target",
                                   recorder_get_sequence_counter());
            return checkpoint_wait_for_command(cmd_fd);
        }
        iohook_enter_replay_mode(recorder_get_sequence_counter());
        recorder_set_fast_forward_live(1, payload);
        return 0;
    }

    _exit(1);  /* unknown opcode */
}

/* ---- Child Initialization ---- */

static void checkpoint_child_command_loop(int cmd_fd, int result_fd,
                                           PyThreadState *saved_tstate) {
    while (1) {
        uint8_t cmd_buf[9];
        ssize_t n = read_all(cmd_fd, cmd_buf, 9);
        if (n <= 0) _exit(0);

        uint8_t opcode = cmd_buf[0];
        uint64_t payload;
        memcpy(&payload, cmd_buf + 1, sizeof(uint64_t));
        payload = pyttd_be64toh(payload);

        if (opcode == 0xFF) _exit(0);  /* DIE */

        /* Re-acquire GIL for Python operations */
        PyEval_RestoreThread(saved_tstate);

        if (opcode == 0x01) {  /* RESUME */
            uint64_t target_seq = payload;
            if (target_seq <= recorder_get_sequence_counter()) {
                serialize_error_result(result_fd, "already_past_target",
                                       recorder_get_sequence_counter());
            } else {
                /* Enter I/O replay mode for deterministic fast-forward */
                iohook_enter_replay_mode(recorder_get_sequence_counter());
                /* Set fast-forward and return to eval hook */
                recorder_set_fast_forward(1, target_seq);
                g_cmd_fd = cmd_fd;
                g_result_fd = result_fd;
                g_saved_tstate = saved_tstate;
                return;  /* return to eval hook */
            }
        } else if (opcode == 0x03) {  /* RESUME_LIVE */
            uint64_t target_seq = payload;
            if (target_seq <= recorder_get_sequence_counter()) {
                serialize_error_result(result_fd, "already_past_target",
                                       recorder_get_sequence_counter());
            } else {
                iohook_enter_replay_mode(recorder_get_sequence_counter());
                recorder_set_fast_forward_live(1, target_seq);
                g_cmd_fd = cmd_fd;
                g_result_fd = result_fd;
                g_saved_tstate = saved_tstate;
                return;  /* return to eval hook — fast-forward + go live */
            }
        } else if (opcode == 0x02) {  /* STEP */
            uint64_t delta = payload;
            if (delta == 0) {
                serialize_target_state(result_fd, -1, NULL);
            } else {
                serialize_error_result(result_fd, "step_before_resume",
                                       recorder_get_sequence_counter());
            }
        }

        saved_tstate = PyEval_SaveThread();
    }
}

static void checkpoint_child_init(int cmd_pipe[2], int result_pipe[2],
                                   int *prior_fds, int n_prior_fds) {
    /* PyOS_AfterFork_Child() was already called by the caller (checkpoint_do_fork)
     * before entering this function — GIL and internal locks are reinitialized. */

    /* 1. Update thread identity */
    g_main_thread_id = PyThread_get_thread_ident();

    /* 3. Disable recording state */
    atomic_store(&g_recording, 0);
    g_flush_thread_created = 0;
    g_inside_repr = 0;  /* TLS — child inherits via fork, reset */
    atomic_store_explicit(&g_last_checkpoint_seq, 0, memory_order_relaxed);
    atomic_store(&g_stop_requested, 0);

    /* 3b. Reset I/O hook replay state (defensive) */
    iohook_reset_child_state();

    /* 4. Signal handling */
    signal(SIGINT, SIG_IGN);
    signal(SIGTERM, SIG_IGN);
    signal(SIGPIPE, SIG_IGN);

    /* 5. Clear inherited trace functions */
    PyEval_SetTrace(NULL, NULL);

    /* 6. Reinitialize pthreads objects */
    g_flush_mutex = (pthread_mutex_t)PTHREAD_MUTEX_INITIALIZER;
    g_flush_cond = (pthread_cond_t)PTHREAD_COND_INITIALIZER;

    /* 7. Free ring buffer memory */
    ringbuf_system_destroy();

    /* 7b. Close inherited binlog file descriptor (parent owns the data) */
    binlog_close_child();

    /* 8. Close inherited file descriptors */
    close(cmd_pipe[1]);     /* child doesn't write to cmd */
    close(result_pipe[0]);  /* child doesn't read from result */
    for (int i = 0; i < n_prior_fds; i++) {
        close(prior_fds[i]);
    }

    /* 9. Clear atexit handlers */
    PyObject *atexit_mod = PyImport_ImportModule("atexit");
    if (atexit_mod) {
        PyObject *r = PyObject_CallMethod(atexit_mod, "_clear", NULL);
        Py_XDECREF(r);
        if (PyErr_Occurred()) PyErr_Clear();
        Py_DECREF(atexit_mod);
    } else {
        PyErr_Clear();
    }

    /* 10. Release GIL and block on command pipe */
    int cmd_fd = cmd_pipe[0];
    int result_fd = result_pipe[1];
    PyThreadState *saved_tstate = PyEval_SaveThread();

    checkpoint_child_command_loop(cmd_fd, result_fd, saved_tstate);
    /* If checkpoint_child_command_loop returns, fast-forward is set —
     * we return to the eval hook caller via the fork() path */
}

/* ---- Fork ---- */

int checkpoint_do_fork(uint64_t sequence_no, PyObject *checkpoint_callback) {
    /* 0. Create pipes */
    int cmd_pipe[2], result_pipe[2];
    if (pipe(cmd_pipe) < 0) return -1;
    if (pipe(result_pipe) < 0) {
        close(cmd_pipe[0]); close(cmd_pipe[1]);
        return -1;
    }

    /* 1. Collect fds from existing checkpoints for child cleanup */
    int prior_fds[MAX_CHECKPOINTS * 2];
    int n_prior_fds = checkpoint_store_get_all_fds(prior_fds);

    /* 2. Pre-fork sync: pause flush thread */
    atomic_store(&g_pause_acked, 0);
    atomic_store(&g_pause_requested, 1);
    pthread_mutex_lock(&g_flush_mutex);
    pthread_cond_signal(&g_flush_cond);       /* wake flush thread if sleeping */
    PyThreadState *saved = PyEval_SaveThread(); /* release GIL */

    struct timespec timeout;
    clock_gettime(CLOCK_REALTIME, &timeout);
    timeout.tv_sec += 1;  /* 1-second timeout */
    while (!atomic_load(&g_pause_acked)) {
        int rc = pthread_cond_timedwait(&g_pause_ack_cv, &g_flush_mutex, &timeout);
        if (rc == ETIMEDOUT) {
            /* Flush thread stuck — skip checkpoint, resume */
            atomic_store(&g_pause_requested, 0);
            pthread_cond_signal(&g_resume_cv);
            pthread_mutex_unlock(&g_flush_mutex);
            PyEval_RestoreThread(saved);
            close(cmd_pipe[0]); close(cmd_pipe[1]);
            close(result_pipe[0]); close(result_pipe[1]);
            return -1;
        }
    }
    pthread_mutex_unlock(&g_flush_mutex);

    /* Re-acquire GIL before fork — flush thread is paused so no contention.
     * This ensures the child inherits a valid GIL state for PyOS_AfterFork_Child. */
    PyEval_RestoreThread(saved);

    /* 3. Pre-fork hooks — puts internal CPython mutexes (including PyMutex-based
     * GIL on 3.13+) into a known state so PyOS_AfterFork_Child can reinit them. */
    PyOS_BeforeFork();

    /* 4. Fork (with GIL held, internal locks in pre-fork state) */
    pid_t pid = fork();
    if (pid < 0) {
        /* Fork failed — undo pre-fork, resume flush thread */
        PyOS_AfterFork_Parent();
        pthread_mutex_lock(&g_flush_mutex);
        atomic_store(&g_pause_requested, 0);
        pthread_cond_signal(&g_resume_cv);
        pthread_mutex_unlock(&g_flush_mutex);
        close(cmd_pipe[0]); close(cmd_pipe[1]);
        close(result_pipe[0]); close(result_pipe[1]);
        return -1;
    }

    if (pid == 0) {
        /* === CHILD PROCESS === */
        PyOS_AfterFork_Child();  /* Reinit GIL, internal locks, thread state */
        checkpoint_child_init(cmd_pipe, result_pipe, prior_fds, n_prior_fds);
        /* If child_init returns, fast-forward was set — the child's call stack
         * unwinds back into checkpoint_do_fork's caller (the eval hook),
         * which proceeds to install trace and call g_original_eval.
         * But we're in the child: the parent already returned from fork().
         * We should NOT return to the eval hook — instead, checkpoint_child_init
         * entered checkpoint_child_command_loop which returned after setting
         * fast-forward. The child now has the GIL and fast-forward is enabled.
         * Return 0 to let the eval hook continue. */
        return 0;
    }

    /* === PARENT PROCESS === */
    PyOS_AfterFork_Parent();  /* Restore internal locks to normal state */

    /* 5. Resume flush thread */
    pthread_mutex_lock(&g_flush_mutex);
    atomic_store(&g_pause_requested, 0);
    pthread_cond_signal(&g_resume_cv);
    pthread_mutex_unlock(&g_flush_mutex);

    /* 6. Close unneeded pipe ends */
    close(cmd_pipe[0]);     /* parent doesn't read cmd */
    close(result_pipe[1]);  /* parent doesn't write result */

    /* 7. Add to checkpoint store */
    int idx = checkpoint_store_add(pid, cmd_pipe[1], result_pipe[0], sequence_no);
    if (idx < 0) {
        /* Store full and eviction failed — kill child */
        uint8_t die[9];
        memset(die, 0, sizeof(die));
        die[0] = 0xFF;
        write_all(cmd_pipe[1], die, 9);
        close(cmd_pipe[1]);
        close(result_pipe[0]);
        waitpid(pid, NULL, 0);
        return -1;
    }

    /* 8. Call Python checkpoint callback (non-fatal on failure) */
    if (checkpoint_callback) {
        PyObject *cb_args = Py_BuildValue("(iK)", (int)pid,
                                       (unsigned long long)sequence_no);
        if (cb_args) {
            PyObject *result = PyObject_Call(checkpoint_callback, cb_args, NULL);
            if (!result) {
                PyErr_WriteUnraisable(checkpoint_callback);
                PyErr_Clear();
            }
            Py_XDECREF(result);
            Py_DECREF(cb_args);
        } else {
            PyErr_Clear();
        }
    }
    return 0;
}

/* Python-facing create_checkpoint (manual trigger, primarily for testing) */
PyObject *pyttd_create_checkpoint(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    /* Only works during recording */
    if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) {
        PyErr_SetString(PyExc_RuntimeError, "Not recording");
        return NULL;
    }
    int rc = checkpoint_do_fork(recorder_get_sequence_counter(), NULL);
    if (rc < 0) {
        PyErr_SetString(PyExc_RuntimeError, "checkpoint_do_fork failed");
        return NULL;
    }
    Py_RETURN_NONE;
}

#else  /* !PYTTD_HAS_FORK */

int checkpoint_do_fork(uint64_t sequence_no, PyObject *checkpoint_callback) {
    (void)sequence_no; (void)checkpoint_callback;
    return PYTTD_ERR_NO_FORK;
}

int checkpoint_wait_for_command(int cmd_fd) {
    (void)cmd_fd;
    return -1;
}

int serialize_target_state(int result_fd, int event_type, PyObject *trace_arg) {
    (void)result_fd; (void)event_type; (void)trace_arg;
    return -1;
}

void serialize_error_result(int result_fd, const char *error_code, uint64_t last_seq) {
    (void)result_fd; (void)error_code; (void)last_seq;
}

PyObject *pyttd_create_checkpoint(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    PyErr_SetString(PyExc_NotImplementedError,
                    "Checkpointing requires fork() (not available on this platform)");
    return NULL;
}

#endif /* PYTTD_HAS_FORK */
