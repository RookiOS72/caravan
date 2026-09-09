// Small POSIX subprocess utility: run a command, capture its stdout,
// and kill it after a fixed duration if it hasn't exited on its own --
// the same "timeboxed, no built-in single-result mode" pattern
// agent/discovery/mdns.py uses for `dns-sd -B`/`-L` (which stream
// forever with no flag to print one result and exit).
#pragma once

#include <string>
#include <vector>

namespace subprocess {

// Runs `args` (argv[0] is the program), waits up to `timeout_seconds`,
// then SIGTERMs it if still running (SIGKILL after a short grace period
// if that doesn't work) and returns whatever it printed to stdout up to
// that point.
std::string run_timeboxed(const std::vector<std::string> & args, double timeout_seconds);

// Starts `args` as a long-running background process (no timeout, no
// output capture) and returns its pid -- caller owns it and must kill()
// it on shutdown. Mirrors mdns.py's advertise_start: `dns-sd -R`
// deregisters on process exit, so this must actually be terminated, not
// just abandoned.
pid_t spawn_background(const std::vector<std::string> & args);

}  // namespace subprocess
