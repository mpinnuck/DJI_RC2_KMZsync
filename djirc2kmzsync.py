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
