#ifndef PYTTD_FRAME_EVENT_H
#define PYTTD_FRAME_EVENT_H

#include <stdint.h>

typedef struct {
    uint64_t sequence_no;
    int line_no;
    int call_depth;
    unsigned long thread_id;    /* PyThread_get_thread_ident() */
    const char *filename;
    const char *function_name;
    const char *event_type;     /* "call", "line", "return", "exception", "exception_unwind" */
    const char *locals_json;    /* serialized repr() of locals, or NULL */
    double timestamp;           /* monotonic clock, seconds since recording start */
} FrameEvent;

#endif /* PYTTD_FRAME_EVENT_H */
