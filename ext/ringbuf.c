#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include "ringbuf.h"

#ifndef _WIN32
#include <pthread.h>
#endif

/* ---- Global Registry ---- */

static ThreadRingBuffer *g_thread_rb_head = NULL;
#ifndef _WIN32
static pthread_mutex_t g_registry_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_key_t g_rb_key;
static int g_rb_key_created = 0;
#endif
static PYTTD_THREAD_LOCAL ThreadRingBuffer *g_my_rb = NULL;
static uint32_t g_per_thread_capacity = PYTTD_PER_THREAD_CAPACITY;
static size_t g_per_thread_pool_size = PYTTD_PER_THREAD_POOL_SIZE;
static int g_system_initialized = 0;

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

/* ---- Thread Destructor ---- */

#ifndef _WIN32
static void thread_rb_destructor(void *arg) {
    ThreadRingBuffer *rb = (ThreadRingBuffer *)arg;
    if (!rb || !rb->initialized) return;
    /* Don't free — mark orphaned so flush thread drains remaining events */
    rb->orphaned = 1;
    g_my_rb = NULL;
}
#endif

/* ---- Per-Thread Buffer Allocation ---- */

static ThreadRingBuffer *alloc_thread_rb(unsigned long thread_id) {
    ThreadRingBuffer *rb = (ThreadRingBuffer *)calloc(1, sizeof(ThreadRingBuffer));
    if (!rb) return NULL;

    uint32_t cap = g_per_thread_capacity;
    if (cap == 0) cap = PYTTD_PER_THREAD_CAPACITY;
    if ((cap & (cap - 1)) != 0) {
        cap = next_power_of_2(cap);
    }

    rb->events = (FrameEvent *)calloc(cap, sizeof(FrameEvent));
    if (!rb->events) {
        free(rb);
        return NULL;
    }

    size_t pool_sz = g_per_thread_pool_size;
    if (pool_sz == 0) pool_sz = PYTTD_PER_THREAD_POOL_SIZE;

    if (pool_init(&rb->pools[0], pool_sz) != 0 ||
        pool_init(&rb->pools[1], pool_sz) != 0) {
        free(rb->events);
        pool_destroy(&rb->pools[0]);
        pool_destroy(&rb->pools[1]);
        free(rb);
        return NULL;
    }

    rb->capacity = cap;
    rb->mask = cap - 1;
    atomic_store_explicit(&rb->head, 0, memory_order_relaxed);
    atomic_store_explicit(&rb->tail, 0, memory_order_relaxed);
    rb->producer_idx = 0;
    rb->dropped_frames = 0;
    rb->pool_overflows = 0;
    rb->thread_id = thread_id;
    rb->initialized = 1;
    rb->orphaned = 0;
    rb->next = NULL;

    return rb;
}

/* ---- Public API: System Lifecycle ---- */

int ringbuf_system_init(uint32_t per_thread_capacity) {
    if (g_system_initialized) {
        ringbuf_system_destroy();
    }

    if (per_thread_capacity == 0) per_thread_capacity = PYTTD_DEFAULT_CAPACITY;
    g_per_thread_capacity = per_thread_capacity;

    /* Use full-size pools for single-thread compat; for additional threads
     * they get the per-thread pool size via alloc_thread_rb */
    g_per_thread_pool_size = PYTTD_STRING_POOL_SIZE;

#ifndef _WIN32
    if (!g_rb_key_created) {
        if (pthread_key_create(&g_rb_key, thread_rb_destructor) != 0) {
            return PYTTD_RINGBUF_ERROR;
        }
        g_rb_key_created = 1;
    }
#endif

    g_thread_rb_head = NULL;
    g_my_rb = NULL;
    g_system_initialized = 1;
    return PYTTD_RINGBUF_OK;
}

void ringbuf_system_destroy(void) {
    if (!g_system_initialized) return;

#ifndef _WIN32
    pthread_mutex_lock(&g_registry_mutex);
#endif
    ThreadRingBuffer *rb = g_thread_rb_head;
    while (rb) {
        ThreadRingBuffer *next = rb->next;
        free(rb->events);
        pool_destroy(&rb->pools[0]);
        pool_destroy(&rb->pools[1]);
        free(rb);
        rb = next;
    }
    g_thread_rb_head = NULL;
#ifndef _WIN32
    pthread_mutex_unlock(&g_registry_mutex);
#endif
    g_my_rb = NULL;
    g_system_initialized = 0;
}

/* ---- Public API: Per-Thread Buffer ---- */

ThreadRingBuffer *ringbuf_get_or_create(unsigned long thread_id) {
    if (g_my_rb && g_my_rb->initialized) return g_my_rb;

    ThreadRingBuffer *rb = alloc_thread_rb(thread_id);
    if (!rb) return NULL;

    /* After the first buffer, use smaller pool sizes for new threads */
#ifndef _WIN32
    pthread_mutex_lock(&g_registry_mutex);
    rb->next = g_thread_rb_head;
    g_thread_rb_head = rb;
    /* Switch to smaller pools for subsequent threads */
    if (g_per_thread_pool_size == PYTTD_STRING_POOL_SIZE) {
        g_per_thread_pool_size = PYTTD_PER_THREAD_POOL_SIZE;
    }
    pthread_mutex_unlock(&g_registry_mutex);
    pthread_setspecific(g_rb_key, rb);
#else
    rb->next = g_thread_rb_head;
    g_thread_rb_head = rb;
    if (g_per_thread_pool_size == PYTTD_STRING_POOL_SIZE) {
        g_per_thread_pool_size = PYTTD_PER_THREAD_POOL_SIZE;
    }
#endif

    g_my_rb = rb;
    return rb;
}

ThreadRingBuffer *ringbuf_get_thread_buffer(void) {
    return g_my_rb;
}

/* ---- Public API: Push/Pop on Specific Buffer ---- */

int ringbuf_push_to(ThreadRingBuffer *rb, const FrameEvent *event) {
    if (!rb || !rb->initialized) return PYTTD_RINGBUF_ERROR;
    uint32_t head = atomic_load_explicit(&rb->head, memory_order_relaxed);
    uint32_t tail = atomic_load_explicit(&rb->tail, memory_order_acquire);

    if (((head + 1) & rb->mask) == (tail & rb->mask)) {
        rb->dropped_frames++;
        return PYTTD_RINGBUF_FULL;
    }

    uint32_t idx = head & rb->mask;
    FrameEvent *slot = &rb->events[idx];
    StringPool *pool = &rb->pools[rb->producer_idx];
    int pool_overflow = 0;

    slot->sequence_no = event->sequence_no;
    slot->line_no = event->line_no;
    slot->call_depth = event->call_depth;
    slot->thread_id = event->thread_id;
    slot->timestamp = event->timestamp;
    slot->event_type = event->event_type;

    slot->filename = pool_copy_string(pool, event->filename);
    if (event->filename && !slot->filename) pool_overflow = 1;

    slot->function_name = pool_copy_string(pool, event->function_name);
    if (event->function_name && !slot->function_name) pool_overflow = 1;

    if (pool_overflow) {
        slot->locals_json = NULL;
        rb->pool_overflows++;
        if (!slot->filename) slot->filename = "<pool_overflow>";
        if (!slot->function_name) slot->function_name = "<pool_overflow>";
    } else {
        slot->locals_json = pool_copy_string(pool, event->locals_json);
        if (event->locals_json && !slot->locals_json) {
            rb->pool_overflows++;
        }
    }

    atomic_store_explicit(&rb->head, head + 1, memory_order_release);
    return pool_overflow ? PYTTD_RINGBUF_POOL_FULL : PYTTD_RINGBUF_OK;
}

int ringbuf_pop_batch_from(ThreadRingBuffer *rb, FrameEvent *out,
                           uint32_t max_count, uint32_t *actual_count) {
    if (!rb || !rb->initialized) {
        if (actual_count) *actual_count = 0;
        return PYTTD_RINGBUF_ERROR;
    }
    uint32_t tail = atomic_load_explicit(&rb->tail, memory_order_relaxed);
    uint32_t head = atomic_load_explicit(&rb->head, memory_order_acquire);

    uint32_t available = head - tail;
    if (available == 0) {
        if (actual_count) *actual_count = 0;
        return PYTTD_RINGBUF_OK;
    }

    uint32_t count = available < max_count ? available : max_count;
    for (uint32_t i = 0; i < count; i++) {
        uint32_t idx = (tail + i) & rb->mask;
        out[i] = rb->events[idx];
    }

    atomic_store_explicit(&rb->tail, tail + count, memory_order_release);
    if (actual_count) *actual_count = count;
    return PYTTD_RINGBUF_OK;
}

/* ---- Pool Management ---- */

void ringbuf_pool_swap_for(ThreadRingBuffer *rb) {
    if (rb) rb->producer_idx = 1 - rb->producer_idx;
}

void ringbuf_pool_reset_consumer_for(ThreadRingBuffer *rb) {
    if (rb) rb->pools[1 - rb->producer_idx].used = 0;
}

/* ---- Registry Queries ---- */

int ringbuf_thread_count(void) {
    int count = 0;
#ifndef _WIN32
    pthread_mutex_lock(&g_registry_mutex);
#endif
    for (ThreadRingBuffer *rb = g_thread_rb_head; rb; rb = rb->next) {
        if (rb->initialized && !rb->orphaned) count++;
    }
#ifndef _WIN32
    pthread_mutex_unlock(&g_registry_mutex);
#endif
    return count;
}

ThreadRingBuffer *ringbuf_get_head(void) {
    return g_thread_rb_head;
}

/* ---- Aggregate Stats ---- */

RingbufStats ringbuf_get_stats(void) {
    RingbufStats stats = {0, 0};
#ifndef _WIN32
    pthread_mutex_lock(&g_registry_mutex);
#endif
    for (ThreadRingBuffer *rb = g_thread_rb_head; rb; rb = rb->next) {
        stats.dropped_frames += rb->dropped_frames;
        stats.pool_overflows += rb->pool_overflows;
    }
#ifndef _WIN32
    pthread_mutex_unlock(&g_registry_mutex);
#endif
    return stats;
}

/* ---- Compatibility Shims ---- */

int ringbuf_push(const FrameEvent *event) {
    if (!g_my_rb) return PYTTD_RINGBUF_ERROR;
    return ringbuf_push_to(g_my_rb, event);
}

uint32_t ringbuf_fill_percent(void) {
    if (!g_my_rb || !g_my_rb->initialized) return 0;
    uint32_t head = atomic_load_explicit(&g_my_rb->head, memory_order_relaxed);
    uint32_t tail = atomic_load_explicit(&g_my_rb->tail, memory_order_relaxed);
    uint32_t used = head - tail;
    if (g_my_rb->capacity == 0) return 0;
    return (uint32_t)((uint64_t)used * 100 / g_my_rb->capacity);
}

char *ringbuf_pool_copy(const char *src) {
    if (!g_my_rb || !g_my_rb->initialized) return NULL;
    return pool_copy_string(&g_my_rb->pools[g_my_rb->producer_idx], src);
}

/* Legacy single-buffer API */
int ringbuf_init(uint32_t capacity) {
    return ringbuf_system_init(capacity);
}

void ringbuf_destroy(void) {
    ringbuf_system_destroy();
}

void ringbuf_pool_swap(void) {
    ringbuf_pool_swap_for(g_my_rb);
}

void ringbuf_pool_reset_consumer(void) {
    ringbuf_pool_reset_consumer_for(g_my_rb);
}

int ringbuf_pop_batch(FrameEvent *out, uint32_t max_count, uint32_t *actual_count) {
    return ringbuf_pop_batch_from(g_my_rb, out, max_count, actual_count);
}
