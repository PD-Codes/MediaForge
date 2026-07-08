#!/usr/bin/env bash
# MediaForge Diagnostics Suite (Linux Shell Menu)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/Log"
mkdir -p "$LOG_DIR"

show_menu() {
    clear
    echo "========================================================================"
    echo "           MediaForge Diagnostics and Testing Suite (Linux)"
    echo "========================================================================"
    echo ""
    echo "  [1] Hardware Encoder, NVENC and VAAPI Diagnostics (encoding/check_nvenc.py)"
    echo "  [2] Open / View Diagnostics Log Directory ($LOG_DIR)"
    echo "  [0] Exit"
    echo ""
    echo "========================================================================"
}

find_python() {
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
    elif command -v python >/dev/null 2>&1; then
        echo "python"
    else
        echo ""
    fi
}

while true; do
    show_menu
    read -r -p "Select an option (0-2): " choice
    case "$choice" in
        1)
            clear
            echo "Starting Hardware Encoder and VAAPI/NVENC Diagnostics..."
            echo ""
            PY_CMD=$(find_python)
            if [ -z "$PY_CMD" ]; then
                echo "[ERROR] Python 3 was not found on this system. Please install python3."
            else
                "$PY_CMD" "$SCRIPT_DIR/encoding/check_nvenc.py"
            fi
            echo ""
            read -r -p "Press [ENTER] to return to the menu..."
            ;;
        2)
            echo "Opening Log Directory ($LOG_DIR)..."
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "$LOG_DIR" >/dev/null 2>&1 &
            else
                echo "Log files in $LOG_DIR:"
                ls -lh "$LOG_DIR"
                read -r -p "Press [ENTER] to continue..."
            fi
            ;;
        0)
            echo "Exiting diagnostics suite."
            exit 0
            ;;
        *)
            echo "Invalid selection. Press [ENTER] to try again..."
            read -r
            ;;
    esac
done
