/*
 * ClickSpeak native launcher — embeds the Python interpreter in-process.
 *
 * macOS grants permissions (Accessibility, Input Monitoring, Microphone)
 * based on the code signature of the running binary.  A script-based
 * launcher (#!/usr/bin/env python3) causes macOS to see "python3" instead
 * of "ClickSpeak", so the app never appears in System Settings > Privacy.
 *
 * This Mach-O binary solves the problem by loading the Python interpreter
 * as a shared library within the same process, so macOS TCC correctly
 * identifies the process as "ClickSpeak".
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

/* ---------- helpers ---------- */

static void show_error(const char *message) {
    char script[4096];
    snprintf(script, sizeof(script),
             "display dialog \"%s\" with title \"ClickSpeak Error\" "
             "buttons \"OK\" default button \"OK\"",
             message);
    pid_t pid = fork();
    if (pid == 0) {
        execlp("osascript", "osascript", "-e", script, NULL);
        _exit(1);
    }
    if (pid > 0) {
        int st;
        waitpid(pid, &st, 0);
    }
}

static const char *find_project_dir(void) {
    static char buf[PATH_MAX];
    const char *env = getenv("CLICKSPEAK_PROJECT_DIR");
    if (env && *env) return env;

    const char *home = getenv("HOME");
    if (!home) return NULL;

    static const char *suffixes[] = {
        "/projects/clickspeak",
        "/clickspeak",
        "/work/clickspeak",
        "/Workspace/clickspeak",
        "/Documents/clickspeak",
    };
    for (size_t i = 0; i < sizeof(suffixes) / sizeof(suffixes[0]); i++) {
        snprintf(buf, sizeof(buf), "%s%s/pyproject.toml", home, suffixes[i]);
        if (access(buf, F_OK) == 0) {
            char *slash = strrchr(buf, '/');
            if (slash) *slash = '\0';
            return buf;
        }
    }
    return NULL;
}

static const char *get_runtime_dir(void) {
    static char buf[PATH_MAX];
    const char *env = getenv("CLICKSPEAK_RUNTIME_DIR");
    if (env && *env) return env;

    const char *home = getenv("HOME");
    if (!home) return NULL;
    snprintf(buf, sizeof(buf),
             "%s/Library/Application Support/ClickSpeak/runtime", home);
    return buf;
}

/* ---------- main ---------- */

int main(int argc, char *argv[]) {
    const char *home = getenv("HOME");
    if (!home) {
        show_error("HOME environment variable is not set.");
        return 1;
    }

    const char *runtime_dir = get_runtime_dir();
    const char *project_dir = find_project_dir();

    if (!runtime_dir) {
        show_error("Could not determine ClickSpeak runtime directory.");
        return 1;
    }

    /* Verify runtime exists */
    char marker[PATH_MAX];
    snprintf(marker, sizeof(marker), "%s/.clickspeak-runtime", runtime_dir);
    if (access(marker, F_OK) != 0) {
        show_error("ClickSpeak runtime not found. Re-run: bash scripts/install_app.sh");
        return 1;
    }

    /* Environment variables */
    setenv("PYTHONUNBUFFERED", "1", 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("PYTHONFAULTHANDLER", "1", 1);
    setenv("CLICKSPEAK_BUNDLE_IDENTIFIER", "com.lamosty.clickspeak", 1);
    setenv("CLICKSPEAK_APP_NAME", "ClickSpeak", 1);
    setenv("CLICKSPEAK_RUNTIME_DIR", runtime_dir, 1);

    /* Own executable path */
    uint32_t exe_size = PATH_MAX;
    char exe_path[PATH_MAX];
    if (_NSGetExecutablePath(exe_path, &exe_size) == 0) {
        setenv("CLICKSPEAK_APP_PATH", exe_path, 1);
    }

    /* Ensure Homebrew paths are in PATH */
    const char *old_path = getenv("PATH");
    char new_path[8192];
    snprintf(new_path, sizeof(new_path), "/opt/homebrew/bin:/usr/local/bin:%s",
             old_path ? old_path : "/usr/bin:/bin");
    setenv("PATH", new_path, 1);

    /* PYTHONPATH: project/src (dev editable install) + runtime site-packages */
    char pythonpath[8192];
    if (project_dir) {
        snprintf(pythonpath, sizeof(pythonpath),
                 "%s/src:%s/lib/python3.12/site-packages",
                 project_dir, runtime_dir);
        chdir(project_dir);
    } else {
        snprintf(pythonpath, sizeof(pythonpath),
                 "%s/lib/python3.12/site-packages", runtime_dir);
    }
    setenv("PYTHONPATH", pythonpath, 1);

    /* ---- Configure embedded Python ---- */
    PyStatus status;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);

    /*
     * Tell Python its executable is the venv's python3 binary.
     * Python then finds <runtime_dir>/pyvenv.cfg, reads the real
     * Python installation path from it, and correctly resolves both
     * the stdlib (from the base install) and site-packages (from the venv).
     * This is exactly how running `<venv>/bin/python3` works normally.
     */
    char venv_python[PATH_MAX];
    snprintf(venv_python, sizeof(venv_python), "%s/bin/python3", runtime_dir);
    wchar_t *w_exe = Py_DecodeLocale(venv_python, NULL);
    if (w_exe) {
        status = PyConfig_SetString(&config, &config.executable, w_exe);
        PyMem_RawFree(w_exe);
        if (PyStatus_Exception(status)) goto fail;
    }

    /*
     * Don't let Python parse argv — our app flags (--check-permissions etc.)
     * are not Python interpreter options.  We set sys.argv manually after init.
     */
    config.parse_argv = 0;
    config.site_import = 1;

    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) goto fail;
    PyConfig_Clear(&config);

    /* Build sys.argv from C argv */
    {
        PyObject *sys_module = PyImport_ImportModule("sys");
        PyObject *argv_list = PyList_New(argc);
        for (int i = 0; i < argc; i++) {
            PyObject *arg = PyUnicode_DecodeFSDefault(argv[i]);
            PyList_SetItem(argv_list, i, arg);  /* steals ref */
        }
        /* argv[0] = 'ClickSpeak' regardless of actual binary path */
        PyObject *name = PyUnicode_FromString("ClickSpeak");
        PyList_SetItem(argv_list, 0, name);
        PyObject_SetAttrString(sys_module, "argv", argv_list);
        Py_DECREF(argv_list);
        Py_DECREF(sys_module);
    }

    /* ---- Run the app ---- */
    int rc = PyRun_SimpleString(
        "from clickspeak.__main__ import main\n"
        "main()\n"
    );

    Py_FinalizeEx();
    return rc ? 1 : 0;

fail:
    fprintf(stderr, "ClickSpeak: Python initialization failed: %s\n",
            status.err_msg ? status.err_msg : "(unknown error)");
    show_error("Python initialization failed. Re-run: bash scripts/install_app.sh");
    PyConfig_Clear(&config);
    return 1;
}
