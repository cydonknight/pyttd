#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "platform.h"
#include "replay.h"
#include "checkpoint_store.h"

#ifdef PYTTD_HAS_FORK

#include <unistd.h>
#include <signal.h>
#include <sys/wait.h>
#include <poll.h>
#include <arpa/inet.h>
#include <string.h>
#include <stdlib.h>
#include <errno.h>

/* Pipe I/O helpers (same as in checkpoint.c — duplicated to avoid link deps) */
static ssize_t replay_write_all(int fd, const void *buf, size_t len) {
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

static ssize_t replay_read_all(int fd, void *buf, size_t len) {
    size_t total = 0;
    while (total < len) {
        ssize_t n = read(fd, (char *)buf + total, len - total);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            return n;
        }
        total += n;
    }
    return (ssize_t)total;
}

PyObject *pyttd_restore_checkpoint(PyObject *self, PyObject *args) {
    (void)self;
    uint64_t target_seq;
    if (!PyArg_ParseTuple(args, "K", &target_seq))
        return NULL;

    int idx = checkpoint_store_find_nearest(target_seq);
    if (idx < 0) {
        PyErr_SetString(PyExc_RuntimeError, "No usable checkpoint found");
        return NULL;
    }

    CheckpointEntry *e = checkpoint_store_get(idx);
    if (!e) {
        PyErr_SetString(PyExc_RuntimeError, "Checkpoint entry is NULL");
        return NULL;
    }
    int cmd_fd = e->cmd_fd;
    int result_fd = e->result_fd;
    int child_pid = e->child_pid;
    e->is_busy = 1;

    /* Build and send RESUME command */
    uint8_t cmd[9];
    cmd[0] = 0x01;
    uint64_t payload = pyttd_htobe64(target_seq);
    memcpy(cmd + 1, &payload, sizeof(uint64_t));

    int rc;
    ssize_t wn;
    Py_BEGIN_ALLOW_THREADS
    wn = replay_write_all(cmd_fd, cmd, 9);
    Py_END_ALLOW_THREADS

    if (wn < 0) {
        e->is_busy = 0;
        /* Write failed — child is likely dead */
        kill(child_pid, SIGKILL);
        waitpid(child_pid, NULL, 0);
        checkpoint_store_evict(idx);
        PyErr_SetString(PyExc_RuntimeError, "Failed to send command to checkpoint child");
        return NULL;
    }

    /* Read result with timeout (5 seconds) */
    Py_BEGIN_ALLOW_THREADS
    struct pollfd pfd = { .fd = result_fd, .events = POLLIN };
    rc = poll(&pfd, 1, 5000);
    Py_END_ALLOW_THREADS

    e->is_busy = 0;

    if (rc <= 0) {
        /* Timeout or error — kill child, remove from store */
        kill(child_pid, SIGKILL);
        waitpid(child_pid, NULL, 0);
        checkpoint_store_evict(idx);
        PyErr_SetString(PyExc_RuntimeError, "Checkpoint child timed out");
        return NULL;
    }

    /* Read length-prefixed JSON result */
    uint32_t net_len;
    ssize_t rn;
    Py_BEGIN_ALLOW_THREADS
    rn = replay_read_all(result_fd, &net_len, 4);
    Py_END_ALLOW_THREADS
    if (rn <= 0) {
        /* Child pipe broken — evict to avoid reusing broken checkpoint */
        kill(child_pid, SIGKILL);
        waitpid(child_pid, NULL, 0);
        checkpoint_store_evict(idx);
        PyErr_SetString(PyExc_RuntimeError, "Failed to read result length from checkpoint child");
        return NULL;
    }
    uint32_t len = ntohl(net_len);

    if (len == 0 || len > 10 * 1024 * 1024) {  /* sanity check: max 10MB */
        kill(child_pid, SIGKILL);
        waitpid(child_pid, NULL, 0);
        checkpoint_store_evict(idx);
        PyErr_SetString(PyExc_RuntimeError, "Invalid result length from checkpoint child");
        return NULL;
    }

    char *buf = (char *)malloc(len + 1);
    if (!buf) {
        /* Pipe has unread data — checkpoint is in corrupted state, must evict */
        kill(child_pid, SIGKILL);
        waitpid(child_pid, NULL, 0);
        checkpoint_store_evict(idx);
        return PyErr_NoMemory();
    }
    Py_BEGIN_ALLOW_THREADS
    rn = replay_read_all(result_fd, buf, len);
    Py_END_ALLOW_THREADS
    if (rn <= 0) {
        free(buf);
        kill(child_pid, SIGKILL);
        waitpid(child_pid, NULL, 0);
        checkpoint_store_evict(idx);
        PyErr_SetString(PyExc_RuntimeError, "Failed to read result data from checkpoint child");
        return NULL;
    }
    buf[len] = '\0';

    /* Update current_position */
    int new_idx = checkpoint_store_find_by_pid(child_pid);
    if (new_idx >= 0) {
        checkpoint_store_update_position(new_idx, target_seq);
    }

    /* Parse JSON to Python dict via json.loads() */
    PyObject *json_mod = PyImport_ImportModule("json");
    if (!json_mod) { free(buf); return NULL; }
    PyObject *result = PyObject_CallMethod(json_mod, "loads", "s", buf);
    Py_DECREF(json_mod);
    free(buf);
    if (!result) return NULL;
    return result;
}

#else  /* !PYTTD_HAS_FORK */

PyObject *pyttd_restore_checkpoint(PyObject *self, PyObject *args) {
    (void)self; (void)args;
    PyErr_SetString(PyExc_NotImplementedError,
                    "Checkpoint restore requires fork() (not available on this platform)");
    return NULL;
}

#endif /* PYTTD_HAS_FORK */
