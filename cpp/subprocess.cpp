#include "subprocess.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <unistd.h>
#include <sys/wait.h>

#include <chrono>
#include <cstring>

namespace subprocess {

namespace {

std::vector<char *> to_argv(const std::vector<std::string> & args) {
    std::vector<char *> argv;
    argv.reserve(args.size() + 1);
    for (auto & a : args) {
        argv.push_back(const_cast<char *>(a.c_str()));
    }
    argv.push_back(nullptr);
    return argv;
}

}  // namespace

std::string run_timeboxed(const std::vector<std::string> & args, double timeout_seconds) {
    int pipe_fds[2];
    if (pipe(pipe_fds) != 0) {
        return "";
    }

    pid_t pid = fork();
    if (pid < 0) {
        close(pipe_fds[0]);
        close(pipe_fds[1]);
        return "";
    }

    if (pid == 0) {
        // child: stdout (and stderr, matching mdns.py's STDOUT merge) -> pipe write end
        dup2(pipe_fds[1], STDOUT_FILENO);
        dup2(pipe_fds[1], STDERR_FILENO);
        close(pipe_fds[0]);
        close(pipe_fds[1]);
        auto argv = to_argv(args);
        execvp(argv[0], argv.data());
        _exit(127);  // exec failed
    }

    // parent
    close(pipe_fds[1]);

    std::string output;
    char buf[4096];
    auto deadline = std::chrono::steady_clock::now() +
                     std::chrono::milliseconds((long)(timeout_seconds * 1000));
    bool timed_out = false;

    while (true) {
        auto remaining = deadline - std::chrono::steady_clock::now();
        auto remaining_ms = std::chrono::duration_cast<std::chrono::milliseconds>(remaining).count();
        if (remaining_ms <= 0) {
            timed_out = true;
            break;
        }
        struct pollfd pfd = { pipe_fds[0], POLLIN, 0 };
        int ret = poll(&pfd, 1, (int)remaining_ms);
        if (ret < 0) {
            break;
        }
        if (ret == 0) {
            timed_out = true;
            break;
        }
        if (pfd.revents & (POLLIN | POLLHUP)) {
            ssize_t n = read(pipe_fds[0], buf, sizeof(buf));
            if (n <= 0) {
                break;  // EOF: process closed its output, likely exited on its own
            }
            output.append(buf, n);
        }
    }

    if (timed_out) {
        kill(pid, SIGTERM);
        // give it a moment to exit cleanly before escalating
        int status;
        for (int i = 0; i < 10; i++) {
            if (waitpid(pid, &status, WNOHANG) == pid) {
                timed_out = false;  // reaped
                break;
            }
            usleep(100 * 1000);
        }
        if (timed_out) {
            kill(pid, SIGKILL);
            waitpid(pid, &status, 0);
        }
        // drain anything left in the pipe after the kill
        ssize_t n;
        while ((n = read(pipe_fds[0], buf, sizeof(buf))) > 0) {
            output.append(buf, n);
        }
    } else {
        int status;
        waitpid(pid, &status, 0);
    }

    close(pipe_fds[0]);
    return output;
}

pid_t spawn_background(const std::vector<std::string> & args) {
    pid_t pid = fork();
    if (pid < 0) {
        return -1;
    }
    if (pid == 0) {
        int devnull = open("/dev/null", O_RDWR);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            close(devnull);
        }
        auto argv = to_argv(args);
        execvp(argv[0], argv.data());
        _exit(127);
    }
    return pid;
}

}  // namespace subprocess
