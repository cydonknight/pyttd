#ifndef PYTTD_PLATFORM_H
#define PYTTD_PLATFORM_H

#include <stdint.h>

#ifdef _WIN32
  /* Windows: no fork support */
#else
  #define PYTTD_HAS_FORK 1
#endif

#define PYTTD_ERR_NO_FORK -1

/* Thread-local storage */
#ifdef _WIN32
  #define PYTTD_THREAD_LOCAL __declspec(thread)
#else
  #define PYTTD_THREAD_LOCAL _Thread_local
#endif

/* 64-bit byte order helpers */
#if defined(__APPLE__)
  #include <libkern/OSByteOrder.h>
  #define pyttd_htobe64(x) OSSwapHostToBigInt64(x)
  #define pyttd_be64toh(x) OSSwapBigToHostInt64(x)
#elif defined(__linux__)
  #include <endian.h>
  #define pyttd_htobe64(x) htobe64(x)
  #define pyttd_be64toh(x) be64toh(x)
#else
  /* Fallback — manual byte swap */
  static inline uint64_t pyttd_htobe64(uint64_t x) {
      return ((x & 0xFF) << 56) | ((x & 0xFF00) << 40) |
             ((x & 0xFF0000) << 24) | ((x & 0xFF000000ULL) << 8) |
             ((x >> 8) & 0xFF000000ULL) | ((x >> 24) & 0xFF0000) |
             ((x >> 40) & 0xFF00) | ((x >> 56) & 0xFF);
  }
  static inline uint64_t pyttd_be64toh(uint64_t x) { return pyttd_htobe64(x); }
#endif

#endif /* PYTTD_PLATFORM_H */
