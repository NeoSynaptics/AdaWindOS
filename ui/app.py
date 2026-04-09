"""Ada UI — opens the chat interface in a browser window.

Works on Windows, macOS, and Linux. Tries Chrome app mode first
for a native-feeling window, falls back to default browser.
"""
import os
import platform
import shutil
import subprocess
import webbrowser

UI_URL = "http://localhost:8765"


def find_chrome():
    """Find Chrome/Chromium across platforms."""
    system = platform.system()

    if system == "Windows":
        paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        for p in paths:
            if os.path.isfile(p):
                return p

    elif system == "Darwin":
        mac_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(mac_path):
            return mac_path

    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            path = shutil.which(name)
            if path:
                return path

    return None


def main():
    browser = find_chrome()

    if browser:
        print(f"Opening Ada in Chrome app mode: {UI_URL}")
        subprocess.Popen([
            browser,
            f"--app={UI_URL}",
            "--window-size=880,700",
            "--disable-background-mode",
            "--disable-extensions",
            "--no-first-run",
        ])
    else:
        print(f"Chrome not found. Opening {UI_URL} in default browser...")
        webbrowser.open(UI_URL)


if __name__ == "__main__":
    main()
