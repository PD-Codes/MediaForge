#!/usr/bin/env bash
# MediaForge Diagnostics Suite (Linux Shell Menu)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$SCRIPT_DIR/Log"
mkdir -p "$LOG_DIR"

show_menu() {
    clear
    echo "========================================================================"
    echo "           MediaForge Diagnostics and Testing Suite (Linux)"
    echo "========================================================================"
    echo ""
    echo "  [1] Hardware Encoder, NVENC and VAAPI Diagnostics (encoding/check_nvenc.py)"
    echo "  [2] Run the Test Suite (pytest)"
    echo "  [3] Run the Repository Checks (static assets, translations, line endings)"
    echo "  [4] Run Everything CI Runs (checks + tests)"
    echo "  [5] Open / View Diagnostics Log Directory ($LOG_DIR)"
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

# Everything below needs an interpreter; bail out with one message instead of
# letting each option fail on its own.
require_python() {
    PY_CMD=$(find_python)
    if [ -z "$PY_CMD" ]; then
        echo "[ERROR] Python 3 was not found on this system. Please install python3."
        return 1
    fi
    return 0
}

run_tests() {
    require_python || return
    if ! "$PY_CMD" -c "import pytest" >/dev/null 2>&1; then
        echo "[ERROR] pytest is not installed. Install the test extra first:"
        echo "        $PY_CMD -m pip install -e \".[test]\""
        return
    fi
    ( cd "$REPO_DIR" && "$PY_CMD" -m pytest -q )
}

run_repo_checks() {
    require_python || return
    ( cd "$REPO_DIR" && "$PY_CMD" .github/scripts/check_repo.py )
}

while true; do
    show_menu
    read -r -p "Select an option (0-5): " choice
    case "$choice" in
        1)
            clear
            echo "Starting Hardware Encoder and VAAPI/NVENC Diagnostics..."
            echo ""
            if require_python; then
                "$PY_CMD" "$SCRIPT_DIR/encoding/check_nvenc.py"
            fi
            echo ""
            read -r -p "Press [ENTER] to return to the menu..."
            ;;
        2)
            clear
            echo "Running the test suite..."
            echo ""
            run_tests
            echo ""
            read -r -p "Press [ENTER] to return to the menu..."
            ;;
        3)
            clear
            echo "Running the repository checks..."
            echo ""
            run_repo_checks
            echo ""
            read -r -p "Press [ENTER] to return to the menu..."
            ;;
        4)
            clear
            echo "Running the repository checks, then the test suite..."
            echo ""
            run_repo_checks
            echo ""
            run_tests
            echo ""
            read -r -p "Press [ENTER] to return to the menu..."
            ;;
        5)
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
