#ifndef PYTTD_RINGBUF_H
#define PYTTD_RINGBUF_H

#include <stdint.h>
#include "frame_event.h"

/* Default configuration */
#define PYTTD_DEFAULT_CAPACITY    65536
#define PYTTD_STRING_POOL_SIZE    (8 * 1024 * 1024)  /* 8MB per pool arena */

/* Return codes */
#define PYTTD_RINGBUF_OK          0
#define PYTTD_RINGBUF_FULL       -1
#define PYTTD_RINGBUF_POOL_FULL  -2
#define PYTTD_RINGBUF_ERROR      -3

/* Initialize ring buffer with given capacity (must be power of 2) */
int ringbuf_init(uint32_t capacity);

/* Push an event into the ring buffer. Copies all strings into string pool.
 * Returns PYTTD_RINGBUF_OK on success, PYTTD_RINGBUF_FULL if buffer full,
 * PYTTD_RINGBUF_POOL_FULL if string pool overflow (event recorded with NULL locals). */
int ringbuf_push(const FrameEvent *event);

/* Pop a batch of events from the ring buffer.
 * out: pre-allocated array, max_count: array size, actual_count: number written.
 * Returns PYTTD_RINGBUF_OK on success. */
int ringbuf_pop_batch(FrameEvent *out, uint32_t max_count, uint32_t *actual_count);

/* Destroy ring buffer and free all memory */
void ringbuf_destroy(void);

/* Copy a string into the current producer pool. Returns pointer to copy, or NULL if pool full. */
char *ringbuf_pool_copy(const char *src);

/* Swap string pools (called after consumer finishes reading a batch) */
void ringbuf_pool_swap(void);

/* Reset the consumer pool (called after swap, before next batch) */
void ringbuf_pool_reset_consumer(void);

/* Get current fill level as percentage (0-100) */
uint32_t ringbuf_fill_percent(void);

/* Statistics */
typedef struct {
    uint64_t dropped_frames;
    uint64_t pool_overflows;
} RingbufStats;

RingbufStats ringbuf_get_stats(void);

#endif /* PYTTD_RINGBUF_H */
