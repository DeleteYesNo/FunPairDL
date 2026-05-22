"""Create a desktop shortcut for FunPairDL with custom icon."""
import os
import sys


def create_shortcut():
    try:
        import winshell
    except ImportError:
        print("Installing winshell...")
        os.system(f"{sys.executable} -m pip install winshell pywin32")
        import winshell

    from win32com.client import Dispatch

    desktop = winshell.desktop()
    shortcut_path = os.path.join(desktop, "FunPairDL.lnk")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(base_dir, "FunPairDL.pyw")
    icon = os.path.join(base_dir, "assets", "funpairdl.ico")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")

    shell = Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(shortcut_path)
    shortcut.TargetPath = pythonw
    shortcut.Arguments = f'"{target}"'
    shortcut.WorkingDirectory = base_dir
    shortcut.IconLocation = icon
    shortcut.Description = "FunPairDL - Download Manager"
    shortcut.save()

    print(f"Shortcut created: {shortcut_path}")
    print(f"  Target: {pythonw} \"{target}\"")
    print(f"  Icon: {icon}")


if __name__ == "__main__":
    create_shortcut()
