import os
import sys
import platform
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def _get_executable_command() -> str:
    """Returns the command string used to launch MediaForge."""
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    else:
        script = os.path.abspath(sys.argv[0])
        return f'"{sys.executable}" "{script}"'

def _set_autostart_windows(enabled: bool):
    import subprocess
    
    startup_folder = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
    shortcut_path = os.path.join(startup_folder, "MediaForge.lnk")
    
    if enabled:
        try:
            target_path = sys.executable
            args = ""
            if getattr(sys, 'frozen', False):
                target_path = sys.executable
            else:
                import shutil
                exe_dir = os.path.dirname(sys.executable)
                possible_exe = os.path.join(exe_dir, "mediaforge.exe")
                if not os.path.exists(possible_exe):
                    possible_exe = os.path.join(exe_dir, "Scripts", "mediaforge.exe")
                
                if os.path.exists(possible_exe):
                    target_path = possible_exe
                else:
                    in_path = shutil.which("mediaforge")
                    if in_path:
                        target_path = in_path
                    else:
                        target_path = sys.executable
                        args = "-m mediaforge"
            
            work_dir = os.getcwd()
            
            ps_script = f"""
$WshShell = New-Object -comObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "{target_path}"
$Shortcut.Arguments = '{args}'
$Shortcut.WorkingDirectory = "{work_dir}"
$Shortcut.WindowStyle = 1
$Shortcut.Save()
"""
            subprocess.run(["powershell", "-Command", ps_script], capture_output=True, check=True)
            logger.info("Added MediaForge to Windows autostart (Startup folder).")
            
            # Remove old registry key if it exists
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            app_name = "MediaForge"
            try:
                registry_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ)
                winreg.DeleteValue(registry_key, app_name)
                winreg.CloseKey(registry_key)
            except Exception:
                pass
                
        except Exception as e:
            logger.error(f"Failed to configure Windows autostart shortcut: {e}")
    else:
        try:
            if os.path.exists(shortcut_path):
                os.remove(shortcut_path)
            logger.info("Removed MediaForge from Windows autostart (Startup folder).")
        except Exception as e:
            logger.error(f"Failed to remove Windows autostart shortcut: {e}")

def _set_autostart_linux(enabled: bool):
    autostart_dir = Path.home() / ".config" / "autostart"
    shortcut_path = autostart_dir / "mediaforge.desktop"
    
    if enabled:
        try:
            autostart_dir.mkdir(parents=True, exist_ok=True)
            desktop_entry = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Version=1.0\n"
                "Name=MediaForge\n"
                "Comment=MediaForge Autostart\n"
                f"Exec={_get_executable_command()}\n"
                "Terminal=false\n"
                "Categories=AudioVideo;Utility;\n"
            )
            shortcut_path.write_text(desktop_entry)
            logger.info(f"Created Linux autostart desktop file at {shortcut_path}")
        except Exception as e:
            logger.error(f"Failed to create Linux autostart desktop file: {e}")
    else:
        if shortcut_path.exists():
            try:
                shortcut_path.unlink()
                logger.info("Removed Linux autostart desktop file.")
            except Exception as e:
                logger.error(f"Failed to remove Linux autostart desktop file: {e}")

def set_autostart(enabled: bool):
    """Enable or disable autostart for Windows or Linux."""
    system = platform.system()
    if system == "Windows":
        _set_autostart_windows(enabled)
    elif system == "Linux":
        if os.path.exists("/.dockerenv") or os.environ.get("MEDIAFORGE_DOCKER") == "1":
            logger.info("Skipping autostart configuration inside Docker.")
            return
        _set_autostart_linux(enabled)
    else:
        logger.warning(f"Autostart is not supported on {system}.")
