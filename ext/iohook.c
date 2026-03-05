#include <Python.h>
#include "iohook.h"

/* Phase 4: Intercept time/random/file for deterministic replay */

PyObject *pyttd_install_io_hooks(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "install_io_hooks not yet implemented");
    return NULL;
}

PyObject *pyttd_remove_io_hooks(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "remove_io_hooks not yet implemented");
    return NULL;
}
