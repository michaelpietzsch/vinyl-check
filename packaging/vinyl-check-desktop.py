"""Entry point of the packaged desktop app (PyInstaller, see vinyl-check.spec).

freeze_support() comes first: the analysis worker is a spawned child process, which a frozen app starts from this very
executable. In that child, freeze_support() runs the worker and never returns, so the window toolkit is not loaded.
"""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from vinylcheck.desktop import main

    main()
