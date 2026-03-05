#ifndef PYTTD_IOHOOK_H
#define PYTTD_IOHOOK_H
#include <Python.h>

PyObject *pyttd_install_io_hooks(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_remove_io_hooks(PyObject *self, PyObject *Py_UNUSED(args));

#endif
