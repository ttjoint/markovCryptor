"""Frozen Tkinter desktop UI.

Moved here when the project moved to a web UI (architecture decision 4). The
logic is unchanged; only the imports were rewritten for the new location. Do not
add features here -- new UI work belongs in the web UI.
"""

from .gui import main as gui_main
from .chat_gui import main as chat_main

__all__ = ["chat_main", "gui_main"]
