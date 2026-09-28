"""pywinhtml - build a Windows desktop app out of one HTML page.

    import pywinhtml as k

    app = k.App("index.html", "#FFFFFF", 900, 600, 700, 480)

    @app.event.on
    def greet(name):
        return "Hello, " + name

    app.run()

This is not published as a package: clone the repo and drop the `pywinhtml/`
folder next to your own code. Renaming the folder is enough to rename the
import - no package name is hard-coded anywhere inside it.
"""

from .core import App, Events, Page
from .win32 import WindowShell

__all__ = ["App", "Page", "Events", "WindowShell"]
__version__ = "0.1.0"
