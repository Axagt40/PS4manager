/* ---------------------------------------------------------------------------
 * PS4 Manager - "connected" notification payload
 *
 * A one-shot payload: it asks the console's kernel to draw a notification and
 * then returns. It writes nothing to the console, changes no setting and
 * touches no file - it only lives in the memory of the process the payload
 * loader (GoldHEN's BinLoader) starts for it.
 *
 * Build it with the makefile next to this file (see README.md in this folder).
 * The SDK provides libkernel.so, whose stub table resolves both kernel calls on
 * the console at runtime, so no firmware address is hard-coded here and the
 * same payload works on every GoldHEN firmware.
 *
 * Copyright (C) 2026 PS4 Manager contributors
 *
 * This program is free software: you can redistribute it and/or modify it
 * under the terms of the GNU General Public License as published by the Free
 * Software Foundation, either version 3 of the License, or (at your option)
 * any later version.
 *
 * This program is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
 * FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
 * more details.  See <https://www.gnu.org/licenses/>.
 * ------------------------------------------------------------------------- */

#include <stddef.h>
#include <stdint.h>

/* The request the kernel expects: eleven fields, then the icon flag, then the
 * text. 0x2D (45) is where the message lives - the same offset the well-known
 * "45 bytes of nothing, then your text" snippets use. Those snippets leave the
 * fields zero, and the kernel then draws nothing at all: target_id = -1 and
 * use_icon_image_uri = 1 with an icon uri are what make the toast appear. */
struct notification_request {
    int32_t type;                /* 0x00 */
    int32_t request_id;          /* 0x04 */
    int32_t priority;            /* 0x08 */
    int32_t message_id;          /* 0x0C */
    int32_t target_id;           /* 0x10 */
    int32_t user_id;             /* 0x14 */
    int32_t unknown1;            /* 0x18 */
    int32_t unknown2;            /* 0x1C */
    int32_t app_id;              /* 0x20 */
    int32_t error_num;           /* 0x24 */
    int32_t unknown3;            /* 0x28 */
    uint8_t use_icon_image_uri;  /* 0x2C */
    char    message[1024];       /* 0x2D */
    char    uri[1024];           /* 0x42D */
    char    unknown_str[1024];   /* 0x82D */
};                               /* 0xC30 = 3120 bytes */

#define NOTIFY_TEXT      "PS4 Manager connected\nby Axagt40"
#define NOTIFY_ICON      "cxml://psnotification/tex_icon_system"

/* Provided by the SDK's libkernel stub table - resolved on the console. */
extern int sceKernelSendNotificationRequest(int device,
                                            const struct notification_request *request,
                                            size_t size, int blocking);
extern int sceKernelDebugOutText(int unknown, const char *text);

/* The whole request as a constant, so the compiler places it in .rodata fully
 * formed: nothing is copied at runtime and no C library is involved. */
static const struct notification_request request = {
    .target_id = -1,
    .use_icon_image_uri = 1,
    .message = NOTIFY_TEXT,
    .uri = NOTIFY_ICON,
};

int main(void) {
    int rc = sceKernelSendNotificationRequest(0, &request, sizeof(request), 0);
    /* One line in the console's log, so "did the payload even run, and what did
     * the kernel say?" is answerable over GoldHEN's klog instead of by guessing. */
    sceKernelDebugOutText(0, rc == 0
                          ? "PS4 Manager: \"connected\" notification sent (rc=0)"
                          : "PS4 Manager: notification call refused (rc!=0)");
    return rc;
}
