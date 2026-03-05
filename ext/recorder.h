#ifndef PYTTD_RECORDER_H
#define PYTTD_RECORDER_H
#include <Python.h>

PyObject *pyttd_start_recording(PyObject *self, PyObject *args, PyObject *kwargs);
PyObject *pyttd_stop_recording(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_get_recording_stats(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_set_ignore_patterns(PyObject *self, PyObject *args);
PyObject *pyttd_request_stop(PyObject *self, PyObject *Py_UNUSED(args));

#endif
