#include <sys/socket.h>
#include <sys/un.h>

#include <fcntl.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "event.h"

#define DEFAULT_EVENT_SOCKET "/run/dialback-zero/events.sock"

void dialback_event(const char *event, int value)
{
  const char *path = getenv("DIALBACK_EVENT_SOCKET");
  struct sockaddr_un address;
  char message[128];
  int fd;
  int flags;
  int length;

  if (event == NULL || event[0] == '\0')
    return;
  if (path == NULL || path[0] == '\0')
    path = DEFAULT_EVENT_SOCKET;
  if (strlen(path) >= sizeof(address.sun_path))
    return;

  fd = socket(AF_UNIX, SOCK_DGRAM, 0);
  if (fd < 0)
    return;
  flags = fcntl(fd, F_GETFL, 0);
  if (flags >= 0)
    (void) fcntl(fd, F_SETFL, flags | O_NONBLOCK);

  memset(&address, 0, sizeof(address));
  address.sun_family = AF_UNIX;
  memcpy(address.sun_path, path, strlen(path) + 1);
  length = snprintf(message, sizeof(message),
                    "{\"event\":\"%s\",\"value\":%d}", event, value);
  if (length > 0 && (size_t) length < sizeof(message)) {
    (void) sendto(fd, message, (size_t) length, MSG_DONTWAIT,
                  (struct sockaddr *) &address,
                  offsetof(struct sockaddr_un, sun_path) + strlen(address.sun_path) + 1);
  }
  close(fd);
}
