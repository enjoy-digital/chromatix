// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Bare metal support for Doom (LiteX picolibc): heap for malloc (main RAM, see main_ram.ld), file
// syscalls (no filesystem).

#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

extern char _heap_start[];
extern char _heap_end[];

static char *heap = _heap_start;

void *sbrk(ptrdiff_t increment)
{
	char *prev = heap;

	if (heap + increment > _heap_end || heap + increment < _heap_start) {
		errno = ENOMEM;
		return (void *)-1;
	}
	heap += increment;
	return prev;
}

/* No filesystem: files can't be opened (config/savegames not stored), console output goes through
   libbase (stdout), not write(). */

int open(const char *path, int flags, ...)
{
	(void)path; (void)flags;
	errno = ENOENT;
	return -1;
}

int close(int fd)
{
	(void)fd;
	return 0;
}

ssize_t read(int fd, void *buf, size_t count)
{
	(void)fd; (void)buf; (void)count;
	errno = EBADF;
	return -1;
}

ssize_t write(int fd, const void *buf, size_t count)
{
	(void)fd; (void)buf; (void)count;
	errno = EBADF;
	return -1;
}

off_t lseek(int fd, off_t offset, int whence)
{
	(void)fd; (void)offset; (void)whence;
	errno = ESPIPE;
	return -1;
}

int mkdir(const char *path, mode_t mode)
{
	(void)path; (void)mode;
	errno = EROFS;
	return -1;
}

int unlink(const char *path)
{
	(void)path;
	errno = EROFS;
	return -1;
}

int rename(const char *old, const char *new)
{
	(void)old; (void)new;
	errno = EROFS;
	return -1;
}

void _exit(int status)
{
	(void)status;
	for (;;);
}
