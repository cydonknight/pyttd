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

/* User pause API (live debugging) */
PyObject *pyttd_request_user_pause(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_user_resume(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_is_user_paused(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_get_sequence_counter(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_flush_and_wait(PyObject *self, PyObject *Py_UNUSED(args));

/* User pause globals exposed for checkpoint child init */
extern _Atomic int g_user_pause_requested;
extern _Atomic int g_user_paused;
extern _Atomic int g_user_pause_thread_count;
extern _Atomic int g_user_pause_expected;

/* Phase 2 getters/setters — used by checkpoint.c and replay.c */
void recorder_set_fast_forward(int enabled, uint64_t target_seq);
void recorder_set_fast_forward_live(int enabled, uint64_t target_seq);
uint64_t recorder_get_sequence_counter(void);
int recorder_get_call_depth(void);
PyObject *pyttd_set_socket_fd(PyObject *self, PyObject *args);
PyObject *pyttd_set_variable(PyObject *self, PyObject *args);

/* Phase 2: RESUME_LIVE support */
extern int g_fast_forward_live;
extern char g_recorder_db_path[1024];
extern char g_resume_live_run_id[33];  /* Parent-provided run_id for branch */

/* Flush thread — exposed for checkpoint_child_go_live */
#ifndef _WIN32
void *flush_thread_func(void *arg);
#endif

/* Phase 2 globals exposed for checkpoint child init */
extern _Atomic int g_recording;
extern _Atomic int g_stop_requested;
extern PYTTD_THREAD_LOCAL int g_inside_repr;
extern int g_flush_thread_created;
extern unsigned long g_main_thread_id;
extern _Atomic uint64_t g_last_checkpoint_seq;
extern _Atomic uint64_t g_sequence_counter;
extern PYTTD_THREAD_LOCAL int g_call_depth;
extern PYTTD_THREAD_LOCAL unsigned long g_my_thread_id;

/* Perf: per-thread code object cache for trace function hot path */
extern PYTTD_THREAD_LOCAL PyFrameObject *g_cached_frame;
extern PYTTD_THREAD_LOCAL PyCodeObject *g_cached_code;
extern PYTTD_THREAD_LOCAL const char *g_cached_filename;
extern PYTTD_THREAD_LOCAL const char *g_cached_funcname;
extern PYTTD_THREAD_LOCAL int g_cached_is_coro;

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
