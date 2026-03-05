#include <Python.h>
#include "platform.h"
#include "checkpoint.h"

/* Phase 2: Fork-based checkpoint manager */

PyObject *pyttd_create_checkpoint(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "create_checkpoint not yet implemented");
    return NULL;
}
