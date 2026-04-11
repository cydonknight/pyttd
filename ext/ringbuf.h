#ifndef PYTTD_RINGBUF_H
#define PYTTD_RINGBUF_H

#include <stdint.h>
#include <stdatomic.h>
#include "frame_event.h"
#include "platform.h"

/* Default configuration */
#define PYTTD_DEFAULT_CAPACITY         65536
#define PYTTD_STRING_POOL_SIZE         (8 * 1024 * 1024)   /* 8MB per pool (main/single-thread compat) */
#define PYTTD_PER_THREAD_CAPACITY      8192
#define PYTTD_PER_THREAD_POOL_SIZE     (2 * 1024 * 1024)   /* 2MB per pool */

/* Return codes */
#define PYTTD_RINGBUF_OK          0
#define PYTTD_RINGBUF_FULL       -1
#define PYTTD_RINGBUF_POOL_FULL  -2
#define PYTTD_RINGBUF_ERROR      -3

/* String pool */
typedef struct {
    char *data;
    size_t capacity;
    size_t used;
} StringPool;

/* Per-thread ring buffer */
typedef struct ThreadRingBuffer {
    FrameEvent *events;
    uint32_t capacity;
    uint32_t mask;
    _Atomic uint32_t head;
    _Atomic uint32_t tail;
    StringPool pools[2];
    int producer_idx;
    uint64_t dropped_frames;
    uint64_t pool_overflows;
    unsigned long thread_id;
    int initialized;
    int orphaned;                      /* set when thread exits, buffer awaits drain */
    struct ThreadRingBuffer *next;     /* linked list */
} ThreadRingBuffer;

/* System lifecycle */
int  ringbuf_system_init(uint32_t per_thread_capacity);
void ringbuf_system_destroy(void);

/* Per-thread buffer (lazy allocation on first push) */
ThreadRingBuffer *ringbuf_get_or_create(unsigned long thread_id);

/* Get current thread's buffer (NULL if not yet created) */
ThreadRingBuffer *ringbuf_get_thread_buffer(void);

/* Push/pop operating on specific buffer */
int  ringbuf_push_to(ThreadRingBuffer *rb, const FrameEvent *event);
int  ringbuf_pop_batch_from(ThreadRingBuffer *rb, FrameEvent *out,
                            uint32_t max_count, uint32_t *actual_count);

/* Pool management for specific buffer */
void ringbuf_pool_swap_for(ThreadRingBuffer *rb);
void ringbuf_pool_reset_consumer_for(ThreadRingBuffer *rb);

/* Mark a specific thread's buffer as orphaned (excludes it from thread_count).
 * Used when the recording thread changes via set_recording_thread() to drop
 * the pre-allocated RPC thread buffer so the checkpoint skip guard doesn't trip. */
void ringbuf_orphan_thread(unsigned long thread_id);

/* Registry queries */
int  ringbuf_thread_count(void);        /* count of non-orphaned buffers */
ThreadRingBuffer *ringbuf_get_head(void); /* for flush iteration */

/* Check if any buffer still has pending events */
int  ringbuf_any_pending(void);

/* Aggregate stats */
typedef struct {
    uint64_t dropped_frames;
    uint64_t pool_overflows;
} RingbufStats;

RingbufStats ringbuf_get_stats(void);

/* Compatibility shim (uses TLS g_my_rb) */
int  ringbuf_push(const FrameEvent *event);
uint32_t ringbuf_fill_percent(void);

/* Copy a string into the current thread's producer pool */
char *ringbuf_pool_copy(const char *src);

/* Legacy API (for backward compatibility — maps to system_init/destroy) */
int ringbuf_init(uint32_t capacity);
void ringbuf_destroy(void);
void ringbuf_pool_swap(void);
void ringbuf_pool_reset_consumer(void);
int ringbuf_pop_batch(FrameEvent *out, uint32_t max_count, uint32_t *actual_count);

#endif /* PYTTD_RINGBUF_H */
