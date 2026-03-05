#include <Python.h>
#include "platform.h"
#include "checkpoint_store.h"

/* Phase 2: Checkpoint index, lifecycle, eviction */

void checkpoint_store_init(void) { /* stub */ }
int checkpoint_store_add(int child_pid, int cmd_fd, int result_fd, uint64_t sequence_no) { return 0; }
int checkpoint_store_find_nearest(uint64_t target_seq) { return -1; }

PyObject *pyttd_kill_all_checkpoints(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "kill_all_checkpoints not yet implemented");
    return NULL;
}
