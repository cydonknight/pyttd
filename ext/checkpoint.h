#ifndef PYTTD_CHECKPOINT_H
#define PYTTD_CHECKPOINT_H
#include <Python.h>
#include <stdint.h>

/* Python-facing: manually trigger a checkpoint (thin wrapper) */
PyObject *pyttd_create_checkpoint(PyObject *self, PyObject *Py_UNUSED(args));

/* Internal: called by eval hook to fork a checkpoint.
 * Returns 0 on success, -1 on failure (caller continues recording). */
int checkpoint_do_fork(uint64_t sequence_no, PyObject *checkpoint_callback);

/* Internal: called by trace/eval hook when fast-forward target is reached.
 * Blocks on cmd_pipe, processes next command, updates fast-forward state.
 * Returns 0 to continue fast-forward, never returns on DIE (_exit). */
int checkpoint_wait_for_command(int cmd_fd);

/* Internal: serialize current frame state to result pipe.
 * event_type: PyTrace_RETURN/PyTrace_EXCEPTION for special locals, -1 for generic.
 * trace_arg: the arg from the trace function (return value or exception tuple). */
int serialize_target_state(int result_fd, int event_type, PyObject *trace_arg);

/* Internal: serialize an error result to the result pipe. */
void serialize_error_result(int result_fd, const char *error_code, uint64_t last_seq);

#endif
