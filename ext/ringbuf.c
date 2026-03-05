#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include "ringbuf.h"

/* ---- String Pool ---- */
typedef struct {
    char *data;
    size_t capacity;
    size_t used;
} StringPool;

/* ---- Ring Buffer State ---- */
static struct {
    FrameEvent *events;
    uint32_t capacity;
    uint32_t mask;          /* capacity - 1 for fast modulo */
    _Atomic uint32_t head;  /* producer writes here */
    _Atomic uint32_t tail;  /* consumer reads here */

    /* Double-buffered string pools: pools[producer_idx] is written by producer,
     * pools[1 - producer_idx] is read by consumer */
    StringPool pools[2];
    int producer_idx;       /* 0 or 1 */

    /* Stats */
    uint64_t dropped_frames;
    uint64_t pool_overflows;

    int initialized;
} g_rb;

/* ---- Helpers ---- */

static uint32_t next_power_of_2(uint32_t v) {
    v--;
    v |= v >> 1;
    v |= v >> 2;
    v |= v >> 4;
    v |= v >> 8;
    v |= v >> 16;
    v++;
    return v;
}

static int pool_init(StringPool *pool, size_t capacity) {
    pool->data = (char *)malloc(capacity);
    if (!pool->data) return -1;
    pool->capacity = capacity;
    pool->used = 0;
    return 0;
}

static void pool_destroy(StringPool *pool) {
    free(pool->data);
    pool->data = NULL;
    pool->capacity = 0;
    pool->used = 0;
}

static char *pool_copy_string(StringPool *pool, const char *src) {
    if (!src) return NULL;
    size_t len = strlen(src) + 1;
    if (pool->used + len > pool->capacity) {
        return NULL;  /* pool overflow */
    }
    char *dst = pool->data + pool->used;
    memcpy(dst, src, len);
    pool->used += len;
    return dst;
}

/* ---- Public API ---- */

int ringbuf_init(uint32_t capacity) {
    if (g_rb.initialized) {
        ringbuf_destroy();
    }

    /* Round up to power of 2 if needed */
    if (capacity == 0) capacity = PYTTD_DEFAULT_CAPACITY;
    if ((capacity & (capacity - 1)) != 0) {
        capacity = next_power_of_2(capacity);
    }

    g_rb.events = (FrameEvent *)calloc(capacity, sizeof(FrameEvent));
    if (!g_rb.events) return PYTTD_RINGBUF_ERROR;

    g_rb.capacity = capacity;
    g_rb.mask = capacity - 1;
    atomic_store_explicit(&g_rb.head, 0, memory_order_relaxed);
    atomic_store_explicit(&g_rb.tail, 0, memory_order_relaxed);

    if (pool_init(&g_rb.pools[0], PYTTD_STRING_POOL_SIZE) != 0 ||
        pool_init(&g_rb.pools[1], PYTTD_STRING_POOL_SIZE) != 0) {
        free(g_rb.events);
        pool_destroy(&g_rb.pools[0]);
        pool_destroy(&g_rb.pools[1]);
        return PYTTD_RINGBUF_ERROR;
    }
    g_rb.producer_idx = 0;
    g_rb.dropped_frames = 0;
    g_rb.pool_overflows = 0;
    g_rb.initialized = 1;
    return PYTTD_RINGBUF_OK;
}

int ringbuf_push(const FrameEvent *event) {
    uint32_t head = atomic_load_explicit(&g_rb.head, memory_order_relaxed);
    uint32_t tail = atomic_load_explicit(&g_rb.tail, memory_order_acquire);

    /* Check if buffer is full */
    if (((head + 1) & g_rb.mask) == (tail & g_rb.mask)) {
        g_rb.dropped_frames++;
        return PYTTD_RINGBUF_FULL;
    }

    uint32_t idx = head & g_rb.mask;
    FrameEvent *slot = &g_rb.events[idx];
    StringPool *pool = &g_rb.pools[g_rb.producer_idx];
    int pool_overflow = 0;

    /* Copy event metadata */
    slot->sequence_no = event->sequence_no;
    slot->line_no = event->line_no;
    slot->call_depth = event->call_depth;
    slot->timestamp = event->timestamp;

    /* event_type is a C string literal, always valid */
    slot->event_type = event->event_type;

    /* Copy strings into pool */
    slot->filename = pool_copy_string(pool, event->filename);
    if (event->filename && !slot->filename) pool_overflow = 1;

    slot->function_name = pool_copy_string(pool, event->function_name);
    if (event->function_name && !slot->function_name) pool_overflow = 1;

    if (pool_overflow) {
        /* Record event but without locals */
        slot->locals_json = NULL;
        g_rb.pool_overflows++;
        /* Still need filename/function_name — use event_type as fallback to ensure non-NULL */
        if (!slot->filename) slot->filename = "<pool_overflow>";
        if (!slot->function_name) slot->function_name = "<pool_overflow>";
    } else {
        slot->locals_json = pool_copy_string(pool, event->locals_json);
        if (event->locals_json && !slot->locals_json) {
            /* Only locals overflowed — still record the frame */
            g_rb.pool_overflows++;
        }
    }

    /* Publish: increment head with release semantics */
    atomic_store_explicit(&g_rb.head, head + 1, memory_order_release);
    return pool_overflow ? PYTTD_RINGBUF_POOL_FULL : PYTTD_RINGBUF_OK;
}

int ringbuf_pop_batch(FrameEvent *out, uint32_t max_count, uint32_t *actual_count) {
    uint32_t tail = atomic_load_explicit(&g_rb.tail, memory_order_relaxed);
    uint32_t head = atomic_load_explicit(&g_rb.head, memory_order_acquire);

    uint32_t available = head - tail;
    if (available == 0) {
        if (actual_count) *actual_count = 0;
        return PYTTD_RINGBUF_OK;
    }

    uint32_t count = available < max_count ? available : max_count;

    for (uint32_t i = 0; i < count; i++) {
        uint32_t idx = (tail + i) & g_rb.mask;
        out[i] = g_rb.events[idx];
    }

    /* Advance tail with release semantics */
    atomic_store_explicit(&g_rb.tail, tail + count, memory_order_release);
    if (actual_count) *actual_count = count;
    return PYTTD_RINGBUF_OK;
}

void ringbuf_destroy(void) {
    if (!g_rb.initialized) return;
    free(g_rb.events);
    g_rb.events = NULL;
    pool_destroy(&g_rb.pools[0]);
    pool_destroy(&g_rb.pools[1]);
    g_rb.initialized = 0;
    g_rb.capacity = 0;
    g_rb.mask = 0;
    atomic_store_explicit(&g_rb.head, 0, memory_order_relaxed);
    atomic_store_explicit(&g_rb.tail, 0, memory_order_relaxed);
}

char *ringbuf_pool_copy(const char *src) {
    return pool_copy_string(&g_rb.pools[g_rb.producer_idx], src);
}

void ringbuf_pool_swap(void) {
    g_rb.producer_idx = 1 - g_rb.producer_idx;
}

void ringbuf_pool_reset_consumer(void) {
    /* Reset the pool that was just consumed (now the non-producer pool) */
    g_rb.pools[1 - g_rb.producer_idx].used = 0;
}

uint32_t ringbuf_fill_percent(void) {
    uint32_t head = atomic_load_explicit(&g_rb.head, memory_order_relaxed);
    uint32_t tail = atomic_load_explicit(&g_rb.tail, memory_order_relaxed);
    uint32_t used = head - tail;
    if (g_rb.capacity == 0) return 0;
    return (uint32_t)((uint64_t)used * 100 / g_rb.capacity);
}

RingbufStats ringbuf_get_stats(void) {
    RingbufStats stats;
    stats.dropped_frames = g_rb.dropped_frames;
    stats.pool_overflows = g_rb.pool_overflows;
    return stats;
}
