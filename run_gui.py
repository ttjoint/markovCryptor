"""Launch the frozen Tkinter GUI (Chinese channel).

The desktop UI is frozen: it still works, but new features land in the web UI
(``python -m markovcryptor.cli serve``). See
``markovcryptor/interfaces/desktop/legacy/``.
"""

from markovcryptor.interfaces.desktop.legacy import gui_main

if __name__ == "__main__":
    gui_main()
