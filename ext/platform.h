#ifndef PYTTD_PLATFORM_H
#define PYTTD_PLATFORM_H

#ifdef _WIN32
  /* Windows: no fork support */
#else
  #define PYTTD_HAS_FORK 1
#endif

#define PYTTD_ERR_NO_FORK -1

#endif /* PYTTD_PLATFORM_H */
