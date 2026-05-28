#---------------------------------------------------------------
#
# DJI RC2 KMZ Sync build commands
# .venv\Scripts\Activate.ps1
# .venv\Scripts\python.exe -m pip install pyinstaller pillow
# Windows build
# .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean DJI_RC2_KMZsync_w.spec
# .venvm/bin/python -m PyInstaller --noconfirm --clean --distpath distm DJI_RC2_KMZsync_m.spec

import tkinter as tk

from config.config_manager import ConfigManager
from viewmodel.sync_viewmodel import SyncViewModel
from view.main_view import MainView


def main():
    config    = ConfigManager()
    viewmodel = SyncViewModel(config)

    root = tk.Tk()
    MainView(root, viewmodel)
    root.mainloop()


if __name__ == "__main__":
    main()
