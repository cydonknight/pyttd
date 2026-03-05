#ifndef PYTTD_CHECKPOINT_STORE_H
#define PYTTD_CHECKPOINT_STORE_H
#include <Python.h>
#include <stdint.h>

/* Internal functions (called by checkpoint.c and replay.c) */
void checkpoint_store_init(void);
int checkpoint_store_add(int child_pid, int cmd_fd, int result_fd, uint64_t sequence_no);
int checkpoint_store_find_nearest(uint64_t target_seq);

/* Python-facing (referenced in PyttdMethods) */
PyObject *pyttd_kill_all_checkpoints(PyObject *self, PyObject *Py_UNUSED(args));

#endif
