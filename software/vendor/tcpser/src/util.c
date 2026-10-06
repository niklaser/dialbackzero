#include <unistd.h>
#include <stdio.h>
#include <errno.h>

#include "util.h"

int writeAll(int fd, const char *data, int len)
{
  int total = 0;

  while (total < len) {
    ssize_t written = write(fd, data + total, (size_t) (len - total));
    if (written > 0) {
      total += (int) written;
      continue;
    }
    if (written < 0 && errno == EINTR)
      continue;
    return total > 0 ? total : -1;
  }
  return total;
}

int writePipe(int fd, char msg)
{
  char tmp[3];

  tmp[0] = msg;
  tmp[1] = '\n';
  tmp[2] = '\0';

  //printf("Writing %c to pipe fd: %d\n",msg,fd);

  return writeAll(fd, tmp, 2);
}

int writeFile(char *name, int fd)
{
  FILE *file;
  char buf[255];
  size_t len;
  size_t size = 1;
  size_t max = 255;

  if (NULL != (file = fopen(name, "rb"))) {
    while (0 < (len = fread(buf, size, max, file))) {
      if (writeAll(fd, buf, (int) len) != (int) len) {
        fclose(file);
        return -1;
      }
    }
    fclose(file);
    return 0;
  }
  return -1;
}
