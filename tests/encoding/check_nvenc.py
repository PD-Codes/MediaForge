#!/usr/bin/env python3
"""
MediaForge Hardware Encoder Diagnostic Tool (General / Multi-Platform)
Checks FFmpeg, NVIDIA NVENC, VAAPI (Linux), VideoToolbox (macOS), and multi-GPU configurations.
Compares the checks from the Web UI (/encoding - 128x128) against the Transcoder (256x256).

- Saves the complete diagnostic log into 'tests/Log/nvenc_diagnostics.log'
- Keeps terminal open when run interactively on Windows
"""

import os
import sys
import shutil
import subprocess
import platform
import datetime

# Attempt to configure Windows console for UTF-8 output
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class TeeLogger:
    """Writes output simultaneously to stdout/terminal and a log file."""
    def __init__(self, filepath):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        self.terminal = sys.stdout
        self.logfile = open(filepath, "w", encoding="utf-8", errors="replace")

    def write(self, message):
        try:
            self.terminal.write(message)
        except UnicodeEncodeError:
            self.terminal.write(message.encode("ascii", errors="replace").decode("ascii"))
        self.logfile.write(message)
        self.flush()

    def flush(self):
        try:
            self.terminal.flush()
        except Exception:
            pass
        self.logfile.flush()

    def close(self):
        self.logfile.close()


def print_header(title):
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def run_cmd(cmd, timeout=12):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except FileNotFoundError:
        return -1, "Command or executable not found."
    except subprocess.TimeoutExpired:
        return -1, "Command execution timed out."
    except Exception as e:
        return -1, str(e)


def check_system_info():
    print_header("1. System & Graphics Card Check")
    print(f"Operating System: {platform.platform()}")
    print(f"Python Version:   {platform.python_version()} ({platform.architecture()[0]})")

    is_windows = platform.system() == "Windows"
    is_linux = platform.system() == "Linux"
    is_mac = platform.system() == "Darwin"

    # ── Windows GPU Check (PowerShell / WMI) ──
    if is_windows:
        _, wmi_out = run_cmd([
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion | Format-List"
        ])
        if wmi_out.strip():
            print("\nDetected Graphics Cards (Windows WMI):")
            print(wmi_out.strip())
        else:
            print("\nCould not query graphics cards via PowerShell.")

    # ── Linux GPU / VAAPI Check ──
    elif is_linux:
        _, lspci_out = run_cmd(["sh", "-c", "lspci | grep -i -E 'vga|3d|display'"])
        if lspci_out.strip():
            print("\nDetected PCI Graphics Devices (lspci):")
            for line in lspci_out.strip().splitlines():
                print(f" -> {line.strip()}")
        vaapi_dev = "/dev/dri/renderD128"
        if os.path.exists(vaapi_dev):
            print(f"\nVAAPI Device Present: {vaapi_dev} (Permissions: {oct(os.stat(vaapi_dev).st_mode)[-3:]})")
        else:
            print(f"\nVAAPI Device Not Found at {vaapi_dev} (No Linux DRM render node detected).")

    # ── macOS GPU Check ──
    elif is_mac:
        _, sp_out = run_cmd(["system_profiler", "SPDisplaysDataType"])
        if sp_out.strip():
            print("\nMac Display & GPU Information:")
            for line in sp_out.strip().splitlines():
                if any(k in line for k in ["Chipset Model:", "VRAM", "Vendor:"]):
                    print(f" -> {line.strip()}")

    # ── NVIDIA-SMI (Cross-Platform) ──
    smi_code, smi_out = run_cmd([
        "nvidia-smi", "--query-gpu=index,name,driver_version,memory.total", "--format=csv,noheader"
    ])
    if smi_code == 0:
        print("\nNVIDIA-SMI GPU Detection:")
        for line in smi_out.strip().splitlines():
            print(f" [GPU {line.strip()}]")
    else:
        print("\nNote: 'nvidia-smi' is not available or failed (No NVIDIA GPU, missing driver, or not running with --gpus inside Docker).")


def check_ffmpeg_capabilities():
    print_header("2. FFmpeg Availability & Compiled Encoders")
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    ret, enc_out = run_cmd([ffmpeg, "-encoders"])
    if ret == -1:
        print("ERROR: 'ffmpeg' binary was not found in system PATH!")
        print(" -> Please ensure FFmpeg is installed and accessible via PATH.")
        return None

    print(f"FFmpeg Executable Path: {shutil.which('ffmpeg') or 'ffmpeg'}")

    encoders_to_check = [
        ("h264_nvenc", "NVIDIA H.264 (NVENC)"),
        ("hevc_nvenc", "NVIDIA H.265 / HEVC (NVENC)"),
        ("h264_vaapi", "Intel/AMD H.264 (VAAPI, Linux)"),
        ("hevc_vaapi", "Intel/AMD H.265 (VAAPI, Linux)"),
        ("h264_videotoolbox", "Apple H.264 (VideoToolbox, macOS)"),
        ("hevc_videotoolbox", "Apple H.265 (VideoToolbox, macOS)"),
        ("libx264", "Software H.264 (CPU)"),
        ("libx265", "Software H.265 (CPU)"),
    ]

    print("\nEncoders Compiled into FFmpeg Build:")
    for enc_name, label in encoders_to_check:
        available = enc_name in enc_out
        status = "YES [OK]" if available else "NO"
        print(f"  {status:8} | {enc_name:18} | {label}")

    # Query Hardware Acceleration Interfaces
    _, hw_out = run_cmd([ffmpeg, "-hwaccels"])
    hw_list = [line.strip() for line in hw_out.splitlines() 
               if line.strip() and not line.lower().startswith("hardware") and not line.lower().startswith("ffmpeg version") and len(line.strip()) < 30]
    print("\nAvailable FFmpeg Hardware Acceleration Methods (-hwaccels):")
    print(" -> " + (", ".join(hw_list) if hw_list else "None"))

    return ffmpeg


def run_mediaforge_tests(ffmpeg):
    print_header("3. MediaForge Transcoder & Encoder Tests")

    is_linux = platform.system() == "Linux"
    is_mac = platform.system() == "Darwin"

    tests = [
        (
            "Test 1: MediaForge Web-UI (/encoding) Check (128x128 pixels)",
            [ffmpeg, "-y", "-f", "lavfi", "-i", "nullsrc=size=128x128:rate=1", "-frames:v", "1", "-an", "-c:v", "h264_nvenc", "-f", "null", "-"]
        ),
        (
            "Test 2: MediaForge Transcoder Check 1 (256x256, hwaccel cuda, preset p1)",
            [ffmpeg, "-y", "-hwaccel", "cuda", "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25", "-t", "1.0", "-vf", "format=yuv420p", "-c:v", "h264_nvenc", "-preset", "p1", "-f", "null", "-"]
        ),
        (
            "Test 3: MediaForge Transcoder Check 2 (256x256, preset p1, no hwaccel)",
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25", "-t", "1.0", "-vf", "format=yuv420p", "-c:v", "h264_nvenc", "-preset", "p1", "-f", "null", "-"]
        ),
        (
            "Test 4: MediaForge Transcoder Check 3 (256x256, legacy preset fast)",
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25", "-t", "1.0", "-vf", "format=yuv420p", "-c:v", "h264_nvenc", "-preset", "fast", "-f", "null", "-"]
        ),
        (
            "Test 5: Multi-GPU / Optimus Check - Explicit GPU 0 (-gpu 0)",
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25", "-t", "0.5", "-c:v", "h264_nvenc", "-gpu", "0", "-f", "null", "-"]
        ),
        (
            "Test 6: Multi-GPU / Optimus Check - Explicit GPU 1 (-gpu 1)",
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25", "-t", "0.5", "-c:v", "h264_nvenc", "-gpu", "1", "-f", "null", "-"]
        )
    ]

    if is_linux:
        tests.append((
            "Test 7: Linux VAAPI Check (renderD128, h264_vaapi)",
            [ffmpeg, "-vaapi_device", "/dev/dri/renderD128", "-f", "lavfi", "-i", "nullsrc=size=256x256:rate=1", "-frames:v", "1", "-an", "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-f", "null", "-"]
        ))
    if is_mac:
        tests.append((
            "Test 7: macOS VideoToolbox Check (h264_videotoolbox)",
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:size=256x256:rate=25", "-t", "0.5", "-c:v", "h264_videotoolbox", "-f", "null", "-"]
        ))

    results = {}
    for title, cmd in tests:
        print(f"\n---> {title}")
        code, out = run_cmd(cmd)
        results[title] = (code, out)
        if code == 0:
            print("     [SUCCESS] Hardware encoder worked successfully for this test!")
        else:
            print("     [FAILED] FFmpeg Error Output:")
            lines = [l.strip() for l in out.splitlines() if any(err in l.lower() for err in ["error", "cannot load", "no nvenc", "driver does not support", "minimum supported", "not supported", "device or resource busy", "no such device", "failed", "invalid argument"])]
            if lines:
                for line in lines[-4:]:
                    print(f"       ! {line}")
            else:
                for line in out.splitlines()[-4:]:
                    print(f"       ! {line}")

    return results


def analyze_results(results):
    print_header("4. Root Cause Analysis & Recommendations")
    if not results:
        return

    test1_code, test1_out = results.get("Test 1: MediaForge Web-UI (/encoding) Check (128x128 pixels)", (-1, ""))
    test4_code, test4_out = results.get("Test 4: MediaForge Transcoder Check 3 (256x256, legacy preset fast)", (-1, ""))
    test2_code, _ = results.get("Test 2: MediaForge Transcoder Check 1 (256x256, hwaccel cuda, preset p1)", (-1, ""))
    test3_code, _ = results.get("Test 3: MediaForge Transcoder Check 2 (256x256, preset p1, no hwaccel)", (-1, ""))

    nvenc_works = any(code == 0 for code in [test2_code, test3_code, test4_code])

    # If Test 1 (128x128 as used on the /encoding settings page) fails, but NVENC works at 256x256:
    if test1_code != 0 and nvenc_works:
        print(">>> ROOT CAUSE DETECTED: RESOLUTION DISCREPANCY IN WEB-UI CHECK (/encoding) <<<")
        print(" -> NVIDIA NVENC hardware encoder requires a minimum resolution of 145x145 pixels on almost all GPUs.")
        print(" -> Test 1 (as implemented in 'src/mediaforge/web/routes/encoding.py') tests with 128x128 pixels.")
        print(" -> Consequently, the /encoding settings page reports 'Only CPU available', even though the")
        print("    actual video transcoder works flawlessly with NVENC during playback (at 256x256+ pixels)!")
        print("\n    SOLUTION: You can manually force select 'nvenc' on the /encoding page despite the warning.")
        print("    For the codebase itself, updating the check resolution in 'encoding.py' from 128x128")
        print("    to 256x256 will fix the hardware detection in the UI.")

    elif "driver does not support the required nvenc api" in test1_out.lower() or "driver does not support" in test4_out.lower():
        print(">>> ROOT CAUSE DETECTED: NVIDIA GRAPHICS DRIVER IS OUTDATED <<<")
        print(" -> The installed FFmpeg binary requires a newer CUDA/NVENC API version than the current driver provides.")
        print(" -> SOLUTION: Update your NVIDIA graphics driver to the latest version directly from nvidia.com")
        print("    (or update the NVIDIA Container Toolkit / host drivers on Linux/Docker setups).")

    elif "cannot load nvcuda.dll" in test1_out.lower() or "cannot load libcuda" in test1_out.lower() or "libcuda.so" in test1_out.lower():
        print(">>> ROOT CAUSE DETECTED: MISSING CUDA LIBRARY (libcuda / nvcuda.dll) <<<")
        print(" -> FFmpeg cannot communicate with the NVIDIA driver.")
        print(" -> On Windows: The NVIDIA driver might be corrupted, or only Microsoft Basic Display Adapter is active.")
        print(" -> On Docker/Linux: Container was started without GPU passthrough flags. Launch with '--gpus all'")
        print("    or configure 'deploy.resources.reservations.devices' in your docker-compose.yaml.")

    elif "no nvenc capable devices found" in test4_out.lower() or "no nvenc capable devices" in test1_out.lower():
        print(">>> ROOT CAUSE DETECTED: NO NVENC-CAPABLE GPU FOUND <<<")
        print(" -> Either the graphics card has no hardware encoder ASIC (e.g., low-end GPUs like GT 630M/MX110/GT 1030),")
        print("    or the GPU architecture is too old to be supported by this modern FFmpeg build.")

    elif nvenc_works:
        print(">>> ALL CHECKS PASSED: NVENC IS FULLY FUNCTIONAL ON THIS SYSTEM! <<<")
        print(" -> Hardware encoding is ready and working correctly.")
        print(" -> If MediaForge still reports only CPU in the UI, verify if MediaForge points to a different")
        print("    FFmpeg binary path or set the encoder explicitly to 'nvenc' in the settings.")
    else:
        print(">>> NOTICE: NVENC HARDWARE ENCODING MIGHT NOT BE AVAILABLE ON THIS SYSTEM <<<")
        print(" -> Please examine the specific FFmpeg error outputs marked with '!' above.")


def main():
    # Store log file inside tests/Log directory
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    log_dir = os.path.join(base_dir, "Log")
    log_file = os.path.join(log_dir, "nvenc_diagnostics.log")

    tee = TeeLogger(log_file)
    sys.stdout = tee

    try:
        print(f"=== MediaForge Hardware Encoder Diagnostics on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
        check_system_info()
        ffmpeg = check_ffmpeg_capabilities()
        if ffmpeg:
            results = run_mediaforge_tests(ffmpeg)
            analyze_results(results)
        print(f"\n[INFO] Complete diagnostic log has been saved to:\n -> {log_file}")
    except Exception as exc:
        print(f"\n[ERROR] Diagnostic tool interrupted: {exc}")
    finally:
        sys.stdout = tee.terminal
        tee.close()
        print("\n" + "-" * 72)
        if sys.platform == "win32" and sys.stdin.isatty():
            try:
                input("Press [ENTER] to close this window...")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    main()
