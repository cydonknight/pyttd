#ifndef PYTTD_REPLAY_H
#define PYTTD_REPLAY_H
#include <Python.h>

PyObject *pyttd_restore_checkpoint(PyObject *self, PyObject *args);
PyObject *pyttd_resume_live(PyObject *self, PyObject *args);

#endif
