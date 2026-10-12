/*
 * LinkWork native messaging host 启动器（C 版）。
 *
 * 为什么需要编译成二进制：
 *   浏览器（Edge/Chrome）在 macOS 上执行一个 shell 脚本时会被系统的安全策略拦下，
 *   表现是「Native host has exited」——进程起来了但立刻死掉，连日志都写不出来。
 *   这台机器上两个能正常工作的原生程序（Codex、夸克）都是 Mach-O 可执行文件，
 *   本文件就是让 LinkWork 走上同一条路。
 *
 * 职责非常窄：
 *   1. 从 argv[1] 取扩展来源（浏览器会传 chrome-extension://<id>/）；
 *   2. 从 argv[0] 所在目录读 host.conf（两行：python=<解释器> / host=<python 脚本>）；
 *   3. 把日志写一行（排查用，永远不写 stdout —— 那是协议通道）；
 *   4. 执行 execv(python, [python, host, origin])，完全接管 stdin/stdout。
 *
 * 安全边界：路径只来自 host.conf（本机文件），不接受命令行传入的任意命令。
 *
 * 编译：scripts/install_native_host.py 会在注册时自动编译；手动编译见该脚本 --help。
 */

#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_LINE 4096
#define MAX_PATH_LEN (PATH_MAX * 2)

static void trim(char *text) {
    if (!text) return;
    size_t len = strlen(text);
    while (len > 0 && (text[len - 1] == '\n' || text[len - 1] == '\r' ||
                       text[len - 1] == ' ' || text[len - 1] == '\t')) {
        text[--len] = '\0';
    }
}

/* 追加一行日志。路径写不进去就静默放弃——绝不能因此影响协议。 */
static void append_log(const char *log_path, const char *fmt, ...) {
    if (!log_path || !*log_path) return;
    FILE *fp = fopen(log_path, "a");
    if (!fp) return;
    time_t now = time(NULL);
    struct tm local;
    localtime_r(&now, &local);
    char stamp[32];
    strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", &local);
    fprintf(fp, "%s [launcher] ", stamp);
    va_list args;
    va_start(args, fmt);
    vfprintf(fp, fmt, args);
    va_end(args);
    fputc('\n', fp);
    fclose(fp);
}

int main(int argc, char *argv[]) {
    char exe_path[MAX_PATH_LEN];
    ssize_t exe_len = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (exe_len > 0) {
        exe_path[exe_len] = '\0';
    } else {
        // macOS 没有 /proc：退回到 argv[0]（浏览器传的是绝对路径）
        snprintf(exe_path, sizeof(exe_path), "%s", argc > 0 ? argv[0] : ".");
    }
    char dir_buf[MAX_PATH_LEN];
    snprintf(dir_buf, sizeof(dir_buf), "%s", exe_path);
    const char *dir = dirname(dir_buf);

    char conf_path[MAX_PATH_LEN];
    snprintf(conf_path, sizeof(conf_path), "%s/host.conf", dir);

    char python[MAX_LINE] = {0};
    char host_script[MAX_LINE] = {0};
    char log_candidates[3][MAX_PATH_LEN];
    int log_count = 0;

    FILE *conf = fopen(conf_path, "r");
    if (!conf) {
        fprintf(stderr, "LinkWork: 读不到配置 %s: %s\n", conf_path, strerror(errno));
        return 1;
    }
    char line[MAX_LINE];
    while (fgets(line, sizeof(line), conf)) {
        trim(line);
        if (strncmp(line, "python=", 7) == 0) {
            snprintf(python, sizeof(python), "%s", line + 7);
        } else if (strncmp(line, "host=", 5) == 0) {
            snprintf(host_script, sizeof(host_script), "%s", line + 5);
        } else if (strncmp(line, "log=", 4) == 0 && log_count < 3) {
            snprintf(log_candidates[log_count], MAX_PATH_LEN, "%s", line + 4);
            log_count++;
        }
    }
    fclose(conf);

    if (!python[0] || !host_script[0]) {
        fprintf(stderr, "LinkWork: host.conf 缺少 python= 或 host= 行（%s）\n", conf_path);
        return 1;
    }

    const char *origin = argc > 1 ? argv[1] : "(无)";

    // 逐个尝试日志落点：第一个能写进去的就用它
    const char *log_path = NULL;
    for (int i = 0; i < log_count; i++) {
        FILE *probe = fopen(log_candidates[i], "a");
        if (probe) {
            fclose(probe);
            log_path = log_candidates[i];
            break;
        }
    }
    append_log(log_path, "启动 host：origin=%s python=%s", origin, python);

    char *const child_argv[] = {python, host_script, (char *)origin, NULL};
    execv(python, child_argv);

    // 只有 execv 失败才会走到这里
    append_log(log_path, "execv 失败：%s（python=%s）", strerror(errno), python);
    fprintf(stderr, "LinkWork: 启动失败 %s: %s\n", python, strerror(errno));
    return 1;
}
