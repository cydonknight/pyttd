#include <Python.h>
#include "platform.h"
#include "checkpoint_store.h"

#ifdef PYTTD_HAS_FORK
#include <unistd.h>
#include <sys/wait.h>
#include <signal.h>
#include <string.h>
#include <errno.h>

static CheckpointEntry g_store[MAX_CHECKPOINTS];
static int g_store_count = 0;  /* total slots in use (including dead) */

/* Pipe I/O helper */
static ssize_t cs_write_all(int fd, const void *buf, size_t len) {
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

void checkpoint_store_init(void) {
    memset(g_store, 0, sizeof(g_store));
    g_store_count = 0;
}

int checkpoint_store_count(void) {
    int count = 0;
    for (int i = 0; i < g_store_count; i++) {
        if (g_store[i].is_alive) count++;
    }
    return count;
}

int checkpoint_store_add(int child_pid, int cmd_fd, int result_fd, uint64_t sequence_no) {
    /* Find a free slot (dead entry or append) */
    int slot = -1;
    for (int i = 0; i < g_store_count; i++) {
        if (!g_store[i].is_alive) {
            slot = i;
            break;
        }
    }

    if (slot < 0) {
        if (g_store_count < MAX_CHECKPOINTS) {
            slot = g_store_count++;
        } else {
            /* Store full — evict one */
            int evict_idx = checkpoint_to_evict();
            if (evict_idx < 0) return -1;
            checkpoint_store_evict(evict_idx);
            slot = evict_idx;
        }
    }

    g_store[slot].child_pid = child_pid;
    g_store[slot].cmd_fd = cmd_fd;
    g_store[slot].result_fd = result_fd;
    g_store[slot].sequence_no = sequence_no;
    g_store[slot].current_position = sequence_no;
    g_store[slot].is_alive = 1;
    g_store[slot].is_busy = 0;
    return slot;
}

int checkpoint_store_find_nearest(uint64_t target_seq) {
    int best_idx = -1;
    uint64_t best_pos = 0;

    for (int i = 0; i < g_store_count; i++) {
        if (!g_store[i].is_alive || g_store[i].is_busy) continue;
        if (g_store[i].current_position <= target_seq) {
            if (best_idx < 0 || g_store[i].current_position > best_pos) {
                best_idx = i;
                best_pos = g_store[i].current_position;
            }
        }
    }
    return best_idx;
}

int checkpoint_store_find_by_pid(int child_pid) {
    for (int i = 0; i < g_store_count; i++) {
        if (g_store[i].is_alive && g_store[i].child_pid == child_pid) {
            return i;
        }
    }
    return -1;
}

void checkpoint_store_update_position(int index, uint64_t new_position) {
    if (index >= 0 && index < g_store_count) {
        g_store[index].current_position = new_position;
    }
}

void checkpoint_store_evict(int index) {
    if (index < 0 || index >= g_store_count) return;
    CheckpointEntry *e = &g_store[index];
    if (!e->is_alive) return;

    /* Send DIE command */
    uint8_t die[9];
    memset(die, 0, sizeof(die));
    die[0] = 0xFF;
    cs_write_all(e->cmd_fd, die, 9);  /* best-effort */

    /* Close pipe fds */
    close(e->cmd_fd);
    close(e->result_fd);

    /* Reap child */
    if (waitpid(e->child_pid, NULL, WNOHANG) == 0) {
        usleep(10000);  /* 10ms grace */
        if (waitpid(e->child_pid, NULL, WNOHANG) == 0) {
            kill(e->child_pid, SIGKILL);
            waitpid(e->child_pid, NULL, 0);
        }
    }

    e->is_alive = 0;
    e->cmd_fd = -1;
    e->result_fd = -1;
}

int checkpoint_to_evict(void) {
    /* Smallest-gap thinning: sort live checkpoints by sequence_no,
     * find the pair with the smallest gap, evict the earlier one.
     * Never evict the most recent checkpoint. */
    int live_indices[MAX_CHECKPOINTS];
    int live_count = 0;

    for (int i = 0; i < g_store_count; i++) {
        if (g_store[i].is_alive && !g_store[i].is_busy) {
            live_indices[live_count++] = i;
        }
    }

    if (live_count < 2) return -1;  /* can't evict the last one */

    /* Sort by sequence_no (insertion sort — trivial for K<=32) */
    for (int i = 1; i < live_count; i++) {
        int key = live_indices[i];
        int j = i - 1;
        while (j >= 0 && g_store[live_indices[j]].sequence_no > g_store[key].sequence_no) {
            live_indices[j + 1] = live_indices[j];
            j--;
        }
        live_indices[j + 1] = key;
    }

    /* Find smallest gap between consecutive checkpoints.
     * Never evict the most recent (last in sorted order). */
    uint64_t min_gap = UINT64_MAX;
    int evict_idx = -1;
    for (int i = 0; i < live_count - 1; i++) {
        uint64_t gap = g_store[live_indices[i + 1]].sequence_no -
                       g_store[live_indices[i]].sequence_no;
        if (gap < min_gap) {
            min_gap = gap;
            evict_idx = live_indices[i];  /* evict earlier of the pair */
        }
    }

    return evict_idx;
}

CheckpointEntry *checkpoint_store_get(int index) {
    if (index < 0 || index >= g_store_count) return NULL;
    return &g_store[index];
}

int checkpoint_store_get_all_fds(int *out_fds) {
    int count = 0;
    for (int i = 0; i < g_store_count; i++) {
        if (g_store[i].is_alive) {
            out_fds[count++] = g_store[i].cmd_fd;
            out_fds[count++] = g_store[i].result_fd;
        }
    }
    return count;
}

PyObject *pyttd_kill_all_checkpoints(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;

    /* Phase 1: Send DIE to all, close pipe fds */
    for (int i = 0; i < g_store_count; i++) {
        if (!g_store[i].is_alive) continue;
        uint8_t die[9];
        memset(die, 0, sizeof(die));
        die[0] = 0xFF;
        cs_write_all(g_store[i].cmd_fd, die, 9);  /* best-effort */
        close(g_store[i].cmd_fd);
        close(g_store[i].result_fd);
    }

    /* Phase 2: Reap all (most already exited) */
    for (int i = 0; i < g_store_count; i++) {
        if (!g_store[i].is_alive) continue;
        if (waitpid(g_store[i].child_pid, NULL, WNOHANG) == 0) {
            usleep(10000);  /* 10ms grace */
            if (waitpid(g_store[i].child_pid, NULL, WNOHANG) == 0) {
                kill(g_store[i].child_pid, SIGKILL);
                waitpid(g_store[i].child_pid, NULL, 0);
            }
        }
        g_store[i].is_alive = 0;
        g_store[i].cmd_fd = -1;
        g_store[i].result_fd = -1;
    }

    Py_RETURN_NONE;
}

PyObject *pyttd_get_checkpoint_count(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    return PyLong_FromLong(checkpoint_store_count());
}

#else  /* !PYTTD_HAS_FORK */

void checkpoint_store_init(void) { }
int checkpoint_store_add(int child_pid, int cmd_fd, int result_fd, uint64_t sequence_no) {
    (void)child_pid; (void)cmd_fd; (void)result_fd; (void)sequence_no;
    return -1;
}
int checkpoint_store_find_nearest(uint64_t target_seq) { (void)target_seq; return -1; }
int checkpoint_store_find_by_pid(int child_pid) { (void)child_pid; return -1; }
void checkpoint_store_update_position(int index, uint64_t new_position) { (void)index; (void)new_position; }
void checkpoint_store_evict(int index) { (void)index; }
int checkpoint_to_evict(void) { return -1; }
CheckpointEntry *checkpoint_store_get(int index) { (void)index; return NULL; }
int checkpoint_store_count(void) { return 0; }
int checkpoint_store_get_all_fds(int *out_fds) { (void)out_fds; return 0; }

PyObject *pyttd_kill_all_checkpoints(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    Py_RETURN_NONE;  /* no-op on Windows */
}

PyObject *pyttd_get_checkpoint_count(PyObject *self, PyObject *Py_UNUSED(args)) {
    (void)self;
    return PyLong_FromLong(0);
}

#endif /* PYTTD_HAS_FORK */
