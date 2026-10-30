# Windows first-run problems

The CLI runs **natively on Windows** (repo on `C:\...`, Windows Python), not
from inside WSL. Docker Desktop's WSL2 backend only hosts the containers.

EDA builds over the bind-mounted checkout may be slower than on Linux-native
storage.

Five first-run traps:

- **Git is older than 2.37.2.** Upgrade Git for Windows from
  [git-scm.com](https://git-scm.com/downloads/win), open a new terminal, and
  rerun `booley init`. Older releases can exhaust their small pool of temporary
  names when Booley stages a large checkout for line-ending repair, so bootstrap
  stops early and names the installed and required versions.
- **`python`/`py` open the Microsoft Store instead of running.** A fresh
  Windows has no real interpreter, only Store aliases. Install one first:
  `winget install Python.Python.3.13`.
- **`docker` isn't found even though Docker Desktop is installed.** Its CLI
  joins `PATH` only after the app has started: launch Docker Desktop, then open
  a **new** terminal before `booley init`. Init treats a missing CLI or stopped
  daemon as fatal and exits before creating or changing project files.
- **The container sees a fully modified tree / phantom diffs.** Git for
  Windows' `core.autocrlf=true` default checks files out with CRLF, which the
  Linux container reads as every file modified. `booley init` fixes it: it sets
  `core.autocrlf=false` and adds an LF default (`* text=auto eol=lf`) without
  overriding attribute rules your repo already has. With Stealth on, the default
  goes in the local `.git/info/attributes`; otherwise init writes it to the root
  `.gitattributes`, which you commit only if you want to share the policy. If
  Doctor then warns `git.attributes-policy-unknown`, see
  [Doctor health](https://booley.dev/docs/doctor/#unknown-git-attributes-policy).

    Init also rewrites files already checked out with CRLF, but only from a
    clean tree. If it reports the repair unsafe, commit or stash your changes
    and rerun:

    ```bash
    booley init
    ```

    `booley doctor` rechecks every run both the RTL repository and, when it is a
    separate repository, the Booley Directory's, and names whichever one is
    unsafe, so a config reset, a fresh clone that drifts back to CRLF, or stale
    index metadata left by an earlier repair gets caught rather than surfacing as
    phantom diffs in the container.

    Don't fix this by hand: `git checkout -- .` leaves the CRLF in place.

- **Init cannot resolve the trusted host Booley executable.** A per-user Python
  install normally places `booley.exe` under the user scripts directory rather
  than the interpreter's system `Scripts` directory. Booley checks both. If the
  error remains, restore the canonical installed Booley launcher (check
  `pipx list` or `uv tool list` for isolated installs), or add its scripts
  directory to `PATH`, open a new terminal, and rerun init.
