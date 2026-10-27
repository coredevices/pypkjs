/* config.h for the Speex codec built into pypkjs: floating point, codec only. */
#ifndef CONFIG_H
#define CONFIG_H

#define FLOATING_POINT 1
#define EXPORT

#ifdef _MSC_VER
#define USE_ALLOCA 1
#else
#define VAR_ARRAYS 1
#endif

#endif /* CONFIG_H */
