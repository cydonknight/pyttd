#ifndef PYTTD_RECORDER_H
#define PYTTD_RECORDER_H
#include <Python.h>
#include <stdint.h>
#include <stdatomic.h>
#include "platform.h"

/* Python-facing functions */
PyObject *pyttd_start_recording(PyObject *self, PyObject *args, PyObject *kwargs);
PyObject *pyttd_stop_recording(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_get_recording_stats(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_set_ignore_patterns(PyObject *self, PyObject *args);
PyObject *pyttd_request_stop(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_set_recording_thread(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_set_secret_patterns(PyObject *self, PyObject *args);
PyObject *pyttd_set_include_patterns(PyObject *self, PyObject *args);
PyObject *pyttd_set_max_frames(PyObject *self, PyObject *args);
PyObject *pyttd_set_file_include_patterns(PyObject *self, PyObject *args);
PyObject *pyttd_set_exclude_patterns(PyObject *self, PyObject *args);
PyObject *pyttd_trace_current_frame(PyObject *self, PyObject *Py_UNUSED(args));

/* Phase 2 getters/setters — used by checkpoint.c and replay.c */
void recorder_set_fast_forward(int enabled, uint64_t target_seq);
uint64_t recorder_get_sequence_counter(void);
int recorder_get_call_depth(void);

/* Phase 2 globals exposed for checkpoint child init */
extern _Atomic int g_recording;
extern _Atomic int g_stop_requested;
extern PYTTD_THREAD_LOCAL int g_inside_repr;
extern int g_flush_thread_created;
extern unsigned long g_main_thread_id;
extern _Atomic uint64_t g_frame_count;
extern _Atomic uint64_t g_last_checkpoint_seq;
extern _Atomic uint64_t g_sequence_counter;
extern PYTTD_THREAD_LOCAL int g_call_depth;

/* Phase 2 child-only globals */
extern int g_cmd_fd;
extern int g_result_fd;
extern PyThreadState *g_saved_tstate;

/* Flush thread synchronization — shared with checkpoint pre-fork sync */
#ifndef _WIN32
#include <pthread.h>
extern pthread_mutex_t g_flush_mutex;
extern pthread_cond_t g_flush_cond;
#endif

/* Locals serialization — exposed for checkpoint target state */
#define MAX_LOCALS_JSON_SIZE   (256 * 1024)
extern char g_locals_buf[MAX_LOCALS_JSON_SIZE];
const char *recorder_serialize_locals(PyObject *frame_obj, char *buf, size_t buf_size,
                                       PyObject *extra_key, PyObject *extra_val);
int recorder_json_escape_string(const char *src, char *dst, size_t dst_size);

#endif
