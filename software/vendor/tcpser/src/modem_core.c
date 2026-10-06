#include <unistd.h>
#include <stdlib.h>     // for atoi
#include <string.h>     // for strtok, strcmp, etc.
#include <ctype.h>      // for toupper
#include <errno.h>
#include <sys/types.h>
#include <sys/wait.h>

#include "getcmd.h"
#include "debug.h"
#include "modem_core.h"
#include "event.h"

#define gen_parity(v) (((((v) * 0x0101010101010101ULL) & 0x8040201008040201ULL) % 0x1FF) & 1)

#define CONFIG_MENU "/usr/local/lib/dialback-zero/config-menu"
#define DIALUP_SOUND "/usr/share/dialback-zero/sounds/dial-up.wav"
#define BUSY_SOUND "/usr/share/dialback-zero/sounds/busy-signal.wav"

char *mdm_responses[37];

static void close_extra_fds(long max_fd)
{
  int fd;

  if (max_fd < 0 || max_fd > 1024)
    max_fd = 1024;
  for (fd = STDERR_FILENO + 1; fd < max_fd; fd++)
    close(fd);
}

int mdm_init()
{
  mdm_responses[MDM_RESP_OK] = "OK";
  mdm_responses[MDM_RESP_RING] = "RING";
  mdm_responses[MDM_RESP_ERROR] = "ERROR";
  mdm_responses[MDM_RESP_CONNECT] = "CONNECT";
  mdm_responses[MDM_RESP_NO_CARRIER] = "NO CARRIER";
  mdm_responses[MDM_RESP_CONNECT_1200] = "CONNECT 1200";
  mdm_responses[MDM_RESP_NO_DIALTONE] = "NO DIALTONE";
  mdm_responses[MDM_RESP_BUSY] = "BUSY";
  mdm_responses[MDM_RESP_NO_ANSWER] = "NO ANSWER";
  mdm_responses[MDM_RESP_CONNECT_0600] = "CONNECT 0600";
  mdm_responses[MDM_RESP_CONNECT_2400] = "CONNECT 2400";
  mdm_responses[MDM_RESP_CONNECT_4800] = "CONNECT 4800";
  mdm_responses[MDM_RESP_CONNECT_9600] = "CONNECT 9600";
  mdm_responses[MDM_RESP_CONNECT_7200] = "CONNECT 7200";
  mdm_responses[MDM_RESP_CONNECT_12000] = "CONNECT 12000";
  mdm_responses[MDM_RESP_CONNECT_14400] = "CONNECT 14400";
  mdm_responses[MDM_RESP_CONNECT_19200] = "CONNECT 19200";
  mdm_responses[MDM_RESP_CONNECT_38400] = "CONNECT 38400";
  mdm_responses[MDM_RESP_CONNECT_57600] = "CONNECT 57600";
  mdm_responses[MDM_RESP_CONNECT_115200] = "CONNECT 115200";
  mdm_responses[MDM_RESP_CONNECT_234000] = "CONNECT 230400";
  return 0;
}

int get_connect_response(int speed, int level)
{
  if (level == 0) {
    return MDM_RESP_CONNECT;
  }
  switch (speed) {
  case 115200:
    return MDM_RESP_CONNECT_115200;
  case 57600:
    return MDM_RESP_CONNECT_57600;
  case 38400:
    return MDM_RESP_CONNECT_38400;
  case 19200:
    return MDM_RESP_CONNECT_19200;
  case 9600:
    return MDM_RESP_CONNECT_9600;
  case 4800:
    return MDM_RESP_CONNECT_4800;
  case 2400:
    return MDM_RESP_CONNECT_2400;
  case 1200:
    return MDM_RESP_CONNECT_1200;
  case 600:
    return MDM_RESP_CONNECT_0600;
  }
  return MDM_RESP_CONNECT;
}

void mdm_init_config(modem_config *cfg)
{
  int i = 0;

  cfg->send_responses = TRUE;
  cfg->connect_response = 0;
  cfg->response_code_level = 4;
  cfg->text_responses = TRUE;
  cfg->echo = TRUE;
  cfg->speaker_setting = 1;
  cfg->cmd_mode = TRUE;
  cfg->conn_type = MDM_CONN_NONE;
  cfg->off_hook = FALSE;
  cfg->line_ringing = FALSE;
  cfg->cur_line_idx = 0;

  for (i = 0; i < 100; i++) {
    cfg->s[i] = 0;
  }
  cfg->s[SRegisterBreak] = 43;
  cfg->s[SRegisterCR] = 13;
  cfg->s[SRegisterLF] = 10;
  cfg->s[SRegisterBackspace] = 8;
  cfg->s[SRegisterBlindWait] = 2;
  cfg->s[SRegisterCarrierWait] = 50;
  cfg->s[SRegisterCommaPause] = 2;
  cfg->s[SRegisterCarrierTime] = 6;
  cfg->s[SRegisterCarrierLoss] = 14;
  cfg->s[SRegisterDTMFTime] = 95;
  cfg->s[SRegisterGuardTime] = 50;

  cfg->crlf[0] = cfg->s[SRegisterCR];
  cfg->crlf[1] = cfg->s[SRegisterLF];
  cfg->crlf[2] = 0;

  cfg->dial_type = 0;
  cfg->last_dial_type = 0;
  cfg->disconnect_delay = 0;

  cfg->pre_break_delay = FALSE;
  cfg->break_len = 0;

  cfg->memory_dial = FALSE;
  cfg->dsr_active = FALSE;
  cfg->dsr_on = TRUE;
  cfg->dcd_on = FALSE;
  cfg->found_a = FALSE;
  cfg->cmd_started = FALSE;
  cfg->allow_transmit = TRUE;
  cfg->invert_dsr = FALSE;
  cfg->invert_dcd = FALSE;

  cfg->config0[0] = '\0';
  cfg->config1[0] = '\0';

  dce_init_config(cfg);
  sh_init_config(cfg);
}

int get_new_cts_state(modem_config *cfg, int up)
{
  return MDM_CL_CTS_HIGH;
}

int get_new_dsr_state(modem_config *cfg, int up)
{
  if (cfg->dsr_on == TRUE)
    return (cfg->invert_dsr == TRUE ? MDM_CL_DSR_LOW : MDM_CL_DSR_HIGH);
  if ((up == TRUE && cfg->invert_dsr == FALSE)
      || (up == FALSE && cfg->invert_dsr == TRUE)
    )
    return MDM_CL_DSR_HIGH;
  else
    return MDM_CL_DSR_LOW;
}

int get_new_dcd_state(modem_config *cfg, int up)
{
  if (cfg->dcd_on == TRUE)
    return (cfg->invert_dcd == TRUE ? MDM_CL_DCD_LOW : MDM_CL_DCD_HIGH);
  if ((up == TRUE && cfg->invert_dcd == FALSE)
      || (up == FALSE && cfg->invert_dcd == TRUE)
    )
    return MDM_CL_DCD_HIGH;
  else
    return MDM_CL_DCD_LOW;
}

int mdm_set_control_lines(modem_config *cfg)
{
  int state = 0;
  int up = (cfg->conn_type == MDM_CONN_NONE ? FALSE : TRUE);

  state |= get_new_cts_state(cfg, up);
  state |= get_new_dsr_state(cfg, up);
  state |= get_new_dcd_state(cfg, up);

  LOG(LOG_INFO, "Control Lines: DSR:%d DCD:%d CTS:%d",
      ((state & MDM_CL_DSR_HIGH) != 0 ? 1 : 0),
      ((state & MDM_CL_DCD_HIGH) != 0 ? 1 : 0), ((state & MDM_CL_CTS_HIGH) != 0 ? 1 : 0)
    );

  dce_set_control_lines(cfg, state);
  return 0;
}

/* Write single char bypassing parity generation since this mirrors input */
void mdm_write_char(modem_config *cfg, char data)
{
  char str[2];

  str[0] = data;
  dce_write(cfg, str, 1);
}

void mdm_write(modem_config *cfg, char *data, int len)
{
  unsigned char *buf;
  int i;
  unsigned int v;

  if (cfg->allow_transmit == TRUE) {
    if (cfg->parity) {
      buf = malloc(len);
      memcpy(buf, data, len);

      for (i = 0; i < len; i++) {
        buf[i] &= 0x7f;
        v = buf[i];
        v = gen_parity(v);
        buf[i] |= (((cfg->parity >> v)) & 1) << 7;
      }
    
      dce_write(cfg, (char *) buf, len);
      free(buf);
    }
    else
      dce_write(cfg, data, len);
  }
}

void mdm_send_response(int msg, modem_config *cfg)
{
  char msgID[17];

  LOG(LOG_DEBUG, "Sending %s response to modem", mdm_responses[msg]);
  if (cfg->send_responses == TRUE) {
    mdm_write(cfg, cfg->crlf, 2);
    if (cfg->text_responses == TRUE) {
      LOG(LOG_ALL, "Sending text response");
      mdm_write(cfg, mdm_responses[msg], strlen(mdm_responses[msg]));
    }
    else {
      LOG(LOG_ALL, "Sending numeric response");
      sprintf(msgID, "%d", msg);
      mdm_write(cfg, msgID, strlen(msgID));
    }
    mdm_write(cfg, cfg->crlf, 2);
  }
}

int mdm_off_hook(modem_config *cfg)
{
  LOG(LOG_INFO, "taking modem off hook");
  if (cfg->off_hook == FALSE)
    dialback_event("offhook", 1);
  cfg->off_hook = TRUE;
  cfg->cmd_mode = FALSE;
  line_off_hook(cfg);
  return 0;
}

int mdm_answer(modem_config *cfg)
{
  if (cfg->line_ringing == TRUE) {
    cfg->conn_type = MDM_CONN_INCOMING;
    dialback_event("connected", 1);
    mdm_off_hook(cfg);
    mdm_set_control_lines(cfg);
    mdm_print_speed(cfg);
  }
  else if (cfg->conn_type == MDM_CONN_INCOMING) {
    // we are connected, just go off hook.
    mdm_off_hook(cfg);
    mdm_set_control_lines(cfg);
  }
  else {
    mdm_disconnect(cfg);
  }
  return 0;
}

int mdm_print_speed(modem_config *cfg)
{
  int speed;

  switch (cfg->connect_response) {
  case 2:
    speed = cfg->dte_speed;
    break;
  default:
    speed = cfg->dce_speed;
    break;

  }
  mdm_send_response(get_connect_response(speed, cfg->response_code_level), cfg);
  return 0;
}

int mdm_connect(modem_config *cfg)
{
  mdm_off_hook(cfg);
  if (cfg->conn_type == MDM_CONN_NONE) {
    if (line_connect(cfg) == 0) {
      cfg->conn_type = MDM_CONN_OUTGOING;
      dialback_event("connected", 1);
      mdm_set_control_lines(cfg);
      mdm_play_sound(cfg, DIALUP_SOUND);
      mdm_print_speed(cfg);
    }
    else {
      mdm_play_sound(cfg, BUSY_SOUND);
      mdm_disconnect(cfg);
      mdm_send_response(MDM_RESP_NO_CARRIER, cfg);
    }
  }
  return 0;
}

int mdm_listen(modem_config *cfg)
{
  return line_listen(cfg);
}

int mdm_disconnect(modem_config *cfg)
{
  int type;
  int was_off_hook;

  LOG_ENTER();
  LOG(LOG_INFO, "Disconnecting modem");
  cfg->cmd_mode = TRUE;
  was_off_hook = cfg->off_hook;
  cfg->break_len = 0;
  cfg->line_ringing = FALSE;
  cfg->pre_break_delay = FALSE;
  if (0 == line_disconnect(cfg)) {
    type = cfg->conn_type;
    cfg->conn_type = MDM_CONN_NONE;
    cfg->off_hook = FALSE;
    mdm_set_control_lines(cfg);
    if (type != MDM_CONN_NONE)
      dialback_event("disconnected", 1);
    if (was_off_hook)
      dialback_event("onhook", 1);
    if (type != MDM_CONN_NONE) {
      mdm_send_response(MDM_RESP_NO_CARRIER, cfg);
      usleep(cfg->disconnect_delay * 1000);
    }
    cfg->rings = 0;
    mdm_listen(cfg);
  }
  else {
    // line still connected.
  }
  LOG_EXIT();
  // force echo on after every disconnect!
  cfg->echo = TRUE; 
  return 0;
}

int mdm_parse_cmd(modem_config *cfg)
{
  int done = FALSE;
  int index = 0;
  int num = 0;
  int start = 0;
  int end = 0;
  int cmd = AT_CMD_NONE;
  char *command = cfg->cur_line;
  char tmp[256];

  LOG_ENTER();

  LOG(LOG_DEBUG, "Evaluating AT%s", command);

  {
    const char *trimmed = command;
    while (isspace((unsigned char) *trimmed))
      trimmed++;
    if (*trimmed == '$') {
      if (mdm_is_config_command(command) && mdm_run_config_menu(cfg) == 0)
        mdm_send_response(MDM_RESP_OK, cfg);
      else
        mdm_send_response(MDM_RESP_ERROR, cfg);
      cfg->cur_line_idx = 0;
      return AT_CMD_END;
    }
  }

  while (TRUE != done) {
    if (cmd != AT_CMD_ERR) {
      cmd = getcmd(command, &index, &num, &start, &end);
      LOG(LOG_DEBUG, "Command: %c (%d), Flags: %d, index=%d, num=%d, data=%d-%d",
          (cmd > -1 ? cmd & 0xff : ' '), cmd, cmd >> 8, index, num, start, end);
    }
    switch (cmd) {
    case AT_CMD_ERR:
      mdm_send_response(MDM_RESP_ERROR, cfg);
      done = TRUE;
      break;
    case AT_CMD_END:
      if (cfg->cmd_mode == TRUE)
        mdm_send_response(MDM_RESP_OK, cfg);
      done = TRUE;
      break;
    case AT_CMD_NONE:
      done = TRUE;
      break;
    case 'O':
    case 'A':
      mdm_answer(cfg);
      cmd = AT_CMD_END;
      done = TRUE;
      break;
    case 'B':  // 212A versus V.22 connection
      if (num > 1) {
        cmd = AT_CMD_ERR;
      }
      else {
        //cfg->connect_1200=num;
      }
      break;

    case 'C':
      /* Hayes carrier-control command. Dialback configuration is AT$CONFIG. */
      if (num < 0 || num > 1)
        cmd = AT_CMD_ERR;
      break;

    case 'D':
      if (end > start) {
        if ((size_t) (end - start) >= sizeof(cfg->dialno) ||
            (size_t) (end - start) >= sizeof(cfg->last_dialno)) {
          cmd = AT_CMD_ERR;
          break;
        }
        memcpy(cfg->dialno, command + start, end - start);
        cfg->dialno[end - start] = '\0';
        cfg->dial_type = (char) num;
        cfg->last_dial_type = (char) num;
        memcpy(cfg->last_dialno, command + start, end - start);
        cfg->last_dialno[end - start] = '\0';
        cfg->memory_dial = FALSE;
      }
      else if (num == 'L') {
        strncpy(cfg->dialno, cfg->last_dialno, sizeof(cfg->dialno) - 1);
        cfg->dialno[sizeof(cfg->dialno) - 1] = '\0';
        cfg->dial_type = cfg->dial_type;
        cfg->memory_dial = TRUE;
        mdm_write(cfg, cfg->crlf, 2);
        mdm_write(cfg, cfg->dialno, strlen(cfg->dialno));
      }
      else {
        cfg->dialno[0] = 0;
        cfg->last_dialno[0] = 0;
        cfg->dial_type = 0;
        cfg->last_dial_type = 0;
      }
      if (strlen(cfg->dialno) > 0) {
        mdm_connect(cfg);
      }
      else {
        mdm_off_hook(cfg);
      }
      done = TRUE;
      break;
    case 'E':  // still need to define #2
      if (num == 0)
        cfg->echo = FALSE;
      else if (num == 1)
        cfg->echo = TRUE;
      else {
        cmd = AT_CMD_ERR;
      }
      break;
    case 'H':
      if (num == 0) {
        mdm_disconnect(cfg);
      }
      else if (num == 1) {
        mdm_answer(cfg);
      }
      else
        cmd = AT_CMD_ERR;
      break;
    case 'J':  // Information.
      mdm_info(cfg);
      break;
    case 'L':  // Speaker volume
      if (num < 1 || num > 3)
        cmd = AT_CMD_ERR;
      else {
        //cfg->volume=num;
      }
      break;
    case 'M':  // speaker settings
      if (num > 3)
        cmd = AT_CMD_ERR;
      else
        cfg->speaker_setting = num;
      break;
    case 'N':  // automode negotiate
      if (num > 1)
        cmd = AT_CMD_ERR;
      else {
        //cfg->auto_mode=num;
      }
      break;
    case 'P':  // defaut to pulse dialing
      //cfg->default_dial_type=MDM_DT_PULSE;
      break;
    case 'Q':  // still need to define #2
      if (num == 0)
        cfg->send_responses = TRUE;
      else if (num == 1)
        cfg->send_responses = FALSE;
      else if (num == 2)        // this should be yes orig/no answer.
        cfg->send_responses = TRUE;
      else {
        cmd = AT_CMD_ERR;
      }
      break;
    case 'S':
      if (num < 0 || num >= (int) (sizeof(cfg->s) / sizeof(cfg->s[0]))) {
        cmd = AT_CMD_ERR;
        break;
      }
      if ((size_t) (end - start) >= sizeof(tmp)) {
        cmd = AT_CMD_ERR;
        break;
      }
      memcpy(tmp, command + start, end - start);
      tmp[end - start] = '\0';
      cfg->s[num] = atoi(tmp);
      if (num == 0)
        dialback_event("autoanswer", cfg->s[0]);
      switch (num) {
      case 3:
        cfg->crlf[0] = cfg->s[SRegisterCR];
        break;
      case 4:
        cfg->crlf[1] = cfg->s[SRegisterLF];
        break;
      }
      break;
    case AT_CMD_FLAG_QUERY | 'S':
      if (num < 0 || num >= (int) (sizeof(cfg->s) / sizeof(cfg->s[0]))) {
        cmd = AT_CMD_ERR;
        break;
      }
      sprintf(tmp, "%s%3.3d", cfg->crlf, cfg->s[num]);
      mdm_write(cfg, tmp, strlen(tmp));
      break;
    case 'T':  // defaut to tone dialing
      //cfg->default_dial_type=MDM_DT_TONE;
      break;
    case 'V':  // done
      if (num == 0)
        cfg->text_responses = FALSE;
      else if (num == 1)
        cfg->text_responses = TRUE;
      else {
        cmd = AT_CMD_ERR;
      }
      break;
    case 'W':
      if (num > -1 && num < 3)
        cfg->connect_response = num;
      else
        cmd = AT_CMD_ERR;
      break;
    case 'X':
      if (num > -1 && num < 5)
        cfg->response_code_level = num;
      else
        cmd = AT_CMD_ERR;
      break;
    case 'Y':  // long space disconnect.
      if (num > 1)
        cmd = AT_CMD_ERR;
      else {
        //cfg->long_disconnect=num;
      }
      break;
    case 'Z':  // long space disconnect.
      if (num > 1)
        cmd = AT_CMD_ERR;
      else {
        // set config0 to cur_line and go.
      }
      break;
    case AT_CMD_FLAG_EXT + 'C':
      switch (num) {
      case 0:
        cfg->dcd_on = TRUE;
        mdm_set_control_lines(cfg);
        break;
      case 1:
        cfg->dcd_on = FALSE;
        mdm_set_control_lines(cfg);
        break;
      default:
        cmd = AT_CMD_ERR;
        break;
      }
      break;
    case AT_CMD_FLAG_EXT + 'K':
      // flow control.
      switch (num) {
      case 0:
        dce_set_flow_control(cfg, 0);
        break;
      case 3:
        dce_set_flow_control(cfg, MDM_FC_RTS);
        break;
      case 4:
        dce_set_flow_control(cfg, MDM_FC_XON);
        break;
      case 5:
        dce_set_flow_control(cfg, MDM_FC_XON);
        // need to add passthrough..  Not sure how.
        break;
      case 6:
        dce_set_flow_control(cfg, MDM_FC_XON | MDM_FC_RTS);
        break;
      default:
        cmd = AT_CMD_ERR;
        break;
      }
      break;
    default:
      break;
    }
  }
  cfg->cur_line_idx = 0;
  return cmd;
}

int detect_parity(modem_config *cfg)
{
  int parity, eobits;
  int charA, charT, charCR;

  charA = cfg->pchars[0];
  charT = cfg->pchars[1];
  charCR = cfg->pchars[2];
  
  parity = (charA & 0x80) >> 7;
  parity |= (charT & 0x80) >> 6;
  parity |= (charCR & 0x80) >> 5;

  eobits = gen_parity(charA & 0x7f);
  eobits |= gen_parity(charT & 0x7f) << 1;
  eobits |= gen_parity(charCR & 0x7f) << 2;

  if (parity == eobits)
    return 2;

  if (parity && (parity ^ eobits) == (parity | eobits))
    return 1;

  return parity & 0x3;
}

int mdm_handle_char(modem_config *cfg, char ch)
{
  int parbit = ch & 0x80;

  ch &= 0x7f;   // ignore parity

  if (cfg->echo == TRUE)
    mdm_write_char(cfg, (ch | parbit));
  if (cfg->cmd_started == TRUE) {
    if (ch == (char) (cfg->s[SRegisterBackspace])) {
      if (cfg->cur_line_idx == 0 && cfg->echo == TRUE) {
        mdm_write(cfg, "T", 1);
      }
      else {
        cfg->cur_line_idx--;
      }
    }
    else if (ch == (char) (cfg->s[SRegisterCR])) {
      // we have a line, process.
      cfg->pchars[2] = ch | parbit;
      cfg->parity = detect_parity(cfg);
      cfg->cur_line[cfg->cur_line_idx] = 0;
      strncpy(cfg->last_cmd, cfg->cur_line, sizeof(cfg->last_cmd) - 1);
      cfg->last_cmd[sizeof(cfg->last_cmd) - 1] = '\0';
      mdm_parse_cmd(cfg);
      cfg->found_a = FALSE;
      cfg->cmd_started = FALSE;
    }
    else {
      if (cfg->cur_line_idx < (int) sizeof(cfg->cur_line) - 1)
        cfg->cur_line[cfg->cur_line_idx++] = ch;
    }
  }
  else if (cfg->found_a == TRUE) {
    if (ch == 't' || ch == 'T') {
      cfg->cmd_started = TRUE;
      LOG(LOG_ALL, "'T' parsed in serial stream, switching to command parse mode");
      cfg->pchars[1] = ch | parbit;
    }
    else if (ch == '/') {
      LOG(LOG_ALL, "'/' parsed in the serial stream, replaying last command");
      cfg->cur_line_idx = strlen(cfg->last_cmd);
      strncpy(cfg->cur_line, cfg->last_cmd, cfg->cur_line_idx);
      mdm_parse_cmd(cfg);
      cfg->cmd_started = FALSE;
    }
    else if (ch != 'a' && ch != 'A') {
      cfg->found_a = FALSE;
    }

  }
  else if (ch == 'a' || ch == 'A') {
    LOG(LOG_ALL, "'A' parsed in serial stream");
    cfg->found_a = TRUE;
    cfg->pchars[0] = ch | parbit;
  }
  return 0;
}

int mdm_clear_break(modem_config *cfg)
{
  cfg->break_len = 0;
  cfg->pre_break_delay = FALSE;
  return 0;
}

int mdm_parse_data(modem_config *cfg, char *data, int len)
{
  int i;
  int ch;

  if (cfg->cmd_mode == TRUE) {
    for (i = 0; i < len; i++) {
      mdm_handle_char(cfg, data[i]);
    }
  }
  else {
    line_write(cfg, data, len);
    if (cfg->pre_break_delay == TRUE) {
      for (i = 0; i < len; i++) {
        ch = data[i];
        if (cfg->parity)
          ch &= 0x7f;
        if (ch == (char) cfg->s[SRegisterBreak]) {
          LOG(LOG_DEBUG, "Break character received");
          cfg->break_len++;
          if (cfg->break_len > 3) {     // more than 3, considered invalid
            cfg->pre_break_delay = FALSE;
            cfg->break_len = 0;
          }
        }
        else {
          LOG(LOG_ALL, "Found non-break character, cancelling break");
          mdm_clear_break(cfg);
        }
      }
    }
  }
  return 0;
}

int mdm_handle_timeout(modem_config *cfg)
{
  if (cfg->pre_break_delay == TRUE && cfg->break_len == 3) {
    // pre and post break.
    LOG(LOG_INFO, "Break condition detected");
    cfg->cmd_mode = TRUE;
    mdm_send_response(MDM_RESP_OK, cfg);
    mdm_clear_break(cfg);
  }
  else if (cfg->pre_break_delay == FALSE) {
    // pre break wait over.
    LOG(LOG_DEBUG, "Initial Break Delay detected");
    cfg->pre_break_delay = TRUE;
  }
  else if (cfg->pre_break_delay == TRUE && cfg->break_len > 0) {
    LOG(LOG_ALL, "Inter-break-char delay time exceeded");
    mdm_clear_break(cfg);
  }
  else if (cfg->s[30] != 0) {
    // timeout...
    LOG(LOG_INFO, "DTE communication inactivity timeout");
    mdm_disconnect(cfg);
  }
  return 0;
}

int mdm_send_ring(modem_config *cfg)
{
  LOG(LOG_DEBUG, "Sending 'RING' to modem");
  cfg->line_ringing = TRUE;
  dialback_event("ring", 1);
  mdm_send_response(MDM_RESP_RING, cfg);
  cfg->rings++;
  LOG(LOG_ALL, "Sent #%d ring", cfg->rings);
  if (cfg->cmd_mode == FALSE || (cfg->s[0] != 0 && cfg->rings >= cfg->s[0])) {
    mdm_answer(cfg);
  }
  return 0;
}

void mdm_info(modem_config *cfg)
{
  static char info[] = "Dialback Zero native modem\r\n";

  LOG(LOG_DEBUG, "Sending info to modem");
  mdm_write(cfg, info, strlen(info));
}

int mdm_is_config_command(const char *command)
{
  static const char expected[] = "$CONFIG";
  size_t i;

  while (isspace((unsigned char) *command))
    command++;
  for (i = 0; expected[i] != '\0'; i++) {
    if (toupper((unsigned char) command[i]) != expected[i])
      return FALSE;
  }
  command += i;
  while (isspace((unsigned char) *command))
    command++;
  return *command == '\0';
}

int mdm_run_config_menu(modem_config *cfg)
{
  const char *program = getenv("DIALBACK_CONFIG_MENU");
  pid_t child;
  int status = 0;
  pid_t waited;
  long max_fd;

  if (cfg->conn_type != MDM_CONN_NONE || cfg->off_hook || !cfg->cmd_mode)
    return -1;
  if (program == NULL || program[0] == '\0')
    program = CONFIG_MENU;
  if (program[0] != '/' || access(program, X_OK) != 0)
    return -1;
  max_fd = sysconf(_SC_OPEN_MAX);

  child = fork();
  if (child < 0)
    return -1;
  if (child == 0) {
    if (dup2(cfg->dce_data.fd, STDIN_FILENO) < 0 ||
        dup2(cfg->dce_data.fd, STDOUT_FILENO) < 0)
      _exit(126);
    close_extra_fds(max_fd);
    execl(program, program, (char *) NULL);
    _exit(127);
  }

  do {
    waited = waitpid(child, &status, 0);
  } while (waited < 0 && errno == EINTR);
  return waited == child && WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}

void mdm_play_sound(modem_config *cfg, const char *path)
{
  const char *mode = getenv("DIALBACK_SOUND_MODE");
  pid_t child;
  int status;
  long max_fd;

  if (cfg->speaker_setting == 0)
    return;
  if (mode != NULL && strcmp(mode, "dial_and_busy") != 0)
    return;
  max_fd = sysconf(_SC_OPEN_MAX);

  child = fork();
  if (child < 0)
    return;
  if (child == 0) {
    pid_t player = fork();
    if (player < 0)
      _exit(126);
    if (player == 0) {
      close_extra_fds(max_fd);
      execl("/usr/bin/aplay", "aplay", "-q", path, (char *) NULL);
      _exit(127);
    }
    _exit(0);
  }
  while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
  }
}
