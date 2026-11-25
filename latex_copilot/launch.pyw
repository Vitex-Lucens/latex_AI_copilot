# Starts Latex-Copilot like an app. The Windows Start-menu and desktop shortcuts run this file with pythonw
# (so there's no console window): it asks for the project folder, then opens the app in the browser.
import os
import sys

sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the folder holding latex_copilot
from latex_copilot.app import main  # noqa: E402

sys.argv[1:1] = ["--launch"]
main()
