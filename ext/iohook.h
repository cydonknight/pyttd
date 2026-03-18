#ifndef PYTTD_IOHOOK_H
#define PYTTD_IOHOOK_H
#include <Python.h>

/* Internal C functions — NOT registered in PyttdMethods */
int install_io_hooks_internal(PyObject *io_flush_callback, PyObject *io_replay_loader);
void remove_io_hooks_internal(void);
void iohook_enter_replay_mode(uint64_t checkpoint_seq);
void iohook_reset_child_state(void);

#endif
