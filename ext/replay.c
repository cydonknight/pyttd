#include <Python.h>
#include "platform.h"
#include "replay.h"

/* Phase 2: Checkpoint resume + fast-forward */

PyObject *pyttd_restore_checkpoint(PyObject *self, PyObject *args) {
    PyErr_SetString(PyExc_NotImplementedError, "restore_checkpoint not yet implemented");
    return NULL;
}
